# mcorechat

Bridge from meshcore device to synapse chat server

## Overview

I didn't like using the web app for chatting with meshcore and I wouldn't consider writing an actual GUI app so I wrote
bridge to extend meshcore to a matrix synapse chat server. This allows you to use a local client like element to chat
with meshcore.

## Features

- mesh2chat: CLI utility to run the bridge

[Discovery] channel in matrix synapse that allows service app users to be indexed in the public directory (bleh!)

## Installation

Installation from scratch is provided here, but you will likely hit some thorny parts along the way. This is a two part
installation and involves first installing a synapse chat server, and then installing the bridge. Knowledge of how to
handle CLI and python pip installs is required for the moment (docker would be nice).

### Install Synapse

Here are the instructions provided by synapse:

* https://matrix-org.github.io/synapse/latest/setup/installation.html

I didn't bother going that route (not for any particular reason) and instead installed from git as follows (change paths
however you see fit)

```bash
* git clone https://github.com/matrix-org/synapse.git ${HOME}/src/synapse
* git -C ${HOME}/src/synapse checkout -b release-v1.146 -t origin/release-v1.146
* mkdir -p ${HOME}/mesh2chat
* python3 -mvenv ${HOME}/mesh2chat/synapse-app
* ${HOME}/mesh2chat/synapse-app/bin/pip install ${HOME}/src/synapse
```

At this point you should have a synapse server installed at ${HOME}/src/synapse.

### Install mesh2chat

Now to install mcorechat. This will install it in the same application directory in home as the synapse server.

```bash
* git clone https://github.com/raincityio/mcorechat.git ${HOME}/src/mcorechat
* mkdir -p ${HOME}/mesh2chat
* python3 -mvenv ${HOME}/mesh2chat/mcorechat
* ${HOME}/mesh2chat/mcorechat/bin/pip install ${HOME}/src/mcorechat
```

## Running

To run synapse, see the examples/run-synapse.sh file.

To run mcorechat, execute the following:

```bash
. ${HOME}/mesh2chat/mcorechat/bin/activate
mcorechat -c ${CONFIG_PATH}
```

## Configuration

Configuration of both synapse and mcorechat is not documented here. Please see the examples directory, a description of
the files in examples follows. They will all probably have to be tweaked to work:

* homeserver.yaml: configuration for synapse server.
* matrix-app.yml: configuration for the service app for synapse, this is the synapse side config for the bridge.
* mesh2chat.yml: configuration for the bridge itself, this is the config you supply to mcorechat.
* mesh2chat_logging.yml: logger configuration if you specify one for mcorechat config.
* run-synapse.sh: example runner for the synapse server if installed with method stated above.

## TODO

* Direct message chats: The old POC backend (still available if you configure mcorechat to use it).
* Feedback for messages that are too long.
* I doubt I'm replaying state correctly for rooms.
* So much more.