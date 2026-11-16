#!/bin/bash
# Open a bash shell in a new container instead of starting uvicorn.
# --rm removes the container on exit; --entrypoint/CMD override skips the app.

docker run -it --rm \
--name sibrun_shell \
-p 8080:8080 \
sibrun bash
