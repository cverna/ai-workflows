"""Redis-queue bridge that drives the OpenCode server.

The BeeAI agents are Redis workers: they pop a ``Task`` from a queue and run a
deterministic workflow. OpenCode is a server, not a worker, so this package
supplies the missing half: it consumes the same queues and drives the server
over its HTTP API, leaving the queue contract unchanged.
"""
