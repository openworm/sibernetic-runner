#!/bin/bash
set -ex

docker run \
--name sibrun_c \
-p 8080:8080 \
sibrun 

