#!/bin/sh

set -e

base=${HOME}/mcorechat/synapse
mkdir -p ${base}
venv=${HOME}/mcorechat/synapse-app

cd ${base}
${venv}/bin/python -msynapse.app.homeserver --config-path ${base}/homeserver.yaml
