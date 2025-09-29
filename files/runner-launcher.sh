#!/bin/bash
set -xe
# TODO: doesn't support org level right now

TOKEN=$1
NAME=$2
URL=$3

if [[ -n "$4" ]]; then
    LABELS="--labels ${4}"
fi

rm -rf ~/actions-runner
mkdir -p ~/actions-runner

tar -zxf actions-runner.tar.gz -C ~/actions-runner

sudo scutil --set HostName $NAME
sudo scutil --set ComputerName $NAME

cd actions-runner
./config.sh --url $URL --unattended --token $TOKEN --name $NAME --ephemeral $LABELS
./run.sh
