# Topics

One topic per line (`-` bullets and bare lines both work). `#` lines are comments.

Cooker takes the first line whose subject has not been researched in the last
`quality.dedup_window_days` (14) days, builds a research chain for it, and marks
it seen. Delete a line to withdraw a topic; add one to queue it.

The queue is also self-refilling: when nothing is live, the daemon seeds from
here, so an empty queue is a request for work rather than a sign of a dead daemon.

## Backlog

- why does a long prefill block every other request on omlx, and what does chunked prefill change
- practical limits of running 70B-class models on 512GB Apple Silicon under continuous load
- how to structure a personal research pipeline so that every artifact cites its sources
- Traefik middlewares worth running in front of self-hosted dashboards
- power draw and thermals of an M3 Ultra under sustained 24/7 background inference
- what a polite background job should do when it detects a human typing
