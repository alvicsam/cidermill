import binascii
import codecs
import datetime
import os
import signal
import subprocess
import sys
import textwrap
import time
import typing
from functools import partial

import httpx
import jwt
import toml
import trio

BASE_DIR = os.path.dirname(__file__)

try:
    with open(os.path.join(BASE_DIR, ".path"), "r") as f:
        os.environ["PATH"] = f.read().strip()
except FileNotFoundError:
    pass


def load_config() -> typing.Dict:
    config = toml.load(os.path.join(BASE_DIR, "config.toml"))["config"]
    with open(os.path.join(BASE_DIR, config["private_key_path"]), "rb") as f:
        signing_key = jwt.jwk_from_pem(f.read())

    config["signing_key"] = signing_key
    if config.get("labels", None) is None:
        config["labels"] = ""
    return config


def log(data):
    print(data, flush=True)


async def generate_jwt(config: typing.Dict) -> str:
    instance = jwt.JWT()
    payload = {
        # Issued at time
        "iat": int(time.time()) - 30,
        # JWT expiration time (10 minutes maximum)
        "exp": int(time.time()) + 600,
        # GitHub App's identifier
        "iss": config["app_id"],
    }
    encoded_jwt = instance.encode(payload, config["signing_key"], alg="RS256")
    return encoded_jwt


async def get_registration_token(config: typing.Dict) -> str:
    log("Requesting new runner registration-token to github ...")
    app_token = await generate_jwt(config)
    transport = httpx.AsyncHTTPTransport(retries=1500)
    async with httpx.AsyncClient(transport=transport) as client:
        res = await client.post(
            f"https://api.github.com/app/installations/{config['installation_id']}/access_tokens",
            headers={
                "Accept": "application/vnd.github.v3+json",
                "Authorization": f"Bearer {app_token}",
            },
        )
        response_data = res.json()
        endpoint_token = response_data["token"]
        res = await client.post(
            f"https://api.github.com/orgs/"
            f"{config['org']}/actions/runners/registration-token",
            headers={
                "Accept": "application/vnd.github.v3+json",
                "Authorization": f"Bearer {endpoint_token}",
            },
        )
        response_data = res.json()
    log(
        f"New registration token is {response_data['token']} and "
        f"expires at {response_data['expires_at']}"
    )
    return response_data["token"]


async def provision_tart_vm(config: typing.Dict):
    random = binascii.hexlify(os.urandom(4)).decode("ascii")
    runner_name = f"{config['runner_base_name']}-{random}"
    log(f"Provisioning: {runner_name}")
    result = await trio.run_process(
        ["tart", "clone", config["base_image"], runner_name]
    )
    if result.returncode != 0:
        log("Failed to clone tart image")
        sys.exit(1)
    result = await trio.run_process(
        [
            "tart",
            "set",
            runner_name,
            "--cpu",
            str(config["cpus"]),
            "--memory",
            str(config["memory"]),
        ]
    )
    if result.returncode != 0:
        log("Failed to set limits on tart image")
        sys.exit(1)

    return runner_name


async def get_tart_ip(runner_name: str, retries: int) -> str:
    attempts = 0
    while attempts < retries:
        try:
            result = await trio.run_process(
                ["tart", "ip", runner_name, "--wait", "3"],
                capture_stdout=True,
                capture_stderr=True,
            )
            break
        except subprocess.CalledProcessError:
            attempts += 1
            continue
    return result.stdout.decode("ascii").strip()


async def startup_checks() -> None:
    try:
        await trio.run_process(
            ["tart", "--version"], capture_stdout=True, capture_stderr=True
        )
    except FileNotFoundError:
        log(
            "Could not find tart. Is homebrew in your PATH? Executing in a non-login"
            f"shell frequently causes this. Your current PATH is: {os.environ['PATH']}"
        )
        sys.exit(1)

    if not os.path.isfile(os.path.join(BASE_DIR, "actions-runner.tar.gz")):
        log("Could not find actions-runner.tar.gz. Check the readme.")
        sys.exit(1)


async def scp_actions_runner(config: typing.Dict, ip: str) -> None:
    log("Copying files to VM")
    await trio.run_process(
        [
            "scp",
            "-i",
            "./id_rsa",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            os.path.join(BASE_DIR, "actions-runner.tar.gz"),
            os.path.join(BASE_DIR, "files", "runner-launcher.sh"),
            f"{config['user']}@{ip}:~/",
        ],
        capture_stdout=True,
        capture_stderr=True,
    )
    log("Copied files to VM")


async def signal_handler():
    with trio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signal_aiter:
        async for _ in signal_aiter:
            raise SystemExit("Received SIGINT or SIGTERM, shutting down")


async def log_output(runner_process: trio.Process, runner_name: str):
    decoder = codecs.getincrementaldecoder("utf8")()
    assert runner_process.stdout is not None
    async for b in runner_process.stdout:
        # Multiple runners may interleave their output so we
        # prefix with runner name.
        log(textwrap.indent(decoder.decode(b), f"{runner_name}: "))


async def run_vm_then_cancel(runner_name: str, cancel_scope):
    try:
        await trio.run_process(
            ["tart", "run", runner_name, "--no-graphics"], check=False
        )
    finally:
        cancel_scope.cancel()


async def run_runner_then_cancel(config: typing.Dict, runner_name: str, cancel_scope):
    try:
        async with trio.open_nursery() as nursery:
            ip = await get_tart_ip(runner_name, 4)
            await scp_actions_runner(config, ip)
            token = await get_registration_token(config)
            log("Launching runner...")
            runner_process: trio.Process = await nursery.start(
                partial(
                    trio.run_process,
                    [
                        "ssh",
                        "-i",
                        "./id_rsa",
                        "-o",
                        "StrictHostKeyChecking=no",
                        "-o",
                        "UserKnownHostsFile=/dev/null",
                        f"{config['user']}@{ip}",
                        f"~/runner-launcher.sh {token} "
                        f"{runner_name} https://github.com/{config['org']} "
                        f"{config['labels']}",
                    ],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
            )
            nursery.start_soon(log_output, runner_process, runner_name)
    finally:
        cancel_scope.cancel()


# This runs a single VM from start to finish, including cleanup.
async def runner(config):
    runner_name = await provision_tart_vm(config)
    try:
        # This starts up both processes, and makes sure that as soon as one of them
        # exits the other also exits. And once both processes are dead, the nursery
        # block exits.
        async with trio.open_nursery() as nursery:
            nursery.start_soon(run_vm_then_cancel, runner_name, nursery.cancel_scope)
            nursery.start_soon(
                run_runner_then_cancel, config, runner_name, nursery.cancel_scope
            )
    finally:
        # shield=True means that the code inside is protected from outside cancellation,
        #  so the run_process call still gets a chance to run even if this whole
        # function is cancelled. So we put a timeout on it, to make sure that it
        # can't hang the whole program.
        with trio.CancelScope(deadline=trio.current_time() + 5, shield=True):
            await trio.run_process(["tart", "delete", runner_name])


async def main(config: typing.Dict):
    await startup_checks()

    async def keep_one_runner_running():
        retries = 0
        while True:
            try:
                await runner(config)
                retries = 0
            except Exception as e:
                log("Exception propagated to top level, restarting runner.")
                log(e)
                retries += 1
                log(f"Sleeping {2 ** retries} seconds before retrying")
                await trio.sleep(2 ** retries)
                continue

    async with trio.open_nursery() as nursery:
        nursery.start_soon(signal_handler)
        for _ in range(config["num_vms"]):
            nursery.start_soon(keep_one_runner_running)


trio.run(main, load_config())
