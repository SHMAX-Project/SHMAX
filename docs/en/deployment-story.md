# First deployment experience

SHMAX v0.1.0 was started on the project owner's existing server. After the update,
readiness checks reported the database, storage and runtime available.
The owner confirmed that the updated installation was working.

That experience led to a simpler installation path: a one-line installer alongside
manual archive installation. Public release acceptance separately covered clean
Debian 13 amd64, updates preserving synthetic data, and rollback after a forced
candidate failure.

[VM acceptance results](https://github.com/SHMAX-Project/SHMAX/actions/runs/38054225801).

This deployment confirmation is not a performance benchmark. Message-volume
statistics, extended load testing and real-world reports covering every attachment
type have not been published yet.
