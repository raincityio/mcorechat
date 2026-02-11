#!/bin/sh

set -e

base=${HOME}/mesh2irc/synapse
mkdir -p ${base}
venv=${HOME}/mesh2irc/synapse-app

cd ${base}
${venv}/bin/python -msynapse.app.homeserver --config-path ${base}/homeserver.yaml
