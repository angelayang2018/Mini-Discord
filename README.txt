Angela Yang 

1. Start the Server
./run.sh --host 127.0.0.1 --tcp-port 9000 --http-port 8080

2. Connect client
python client.py --host 127.0.0.1 --port 9000

| Requirement               | Video Timestamp |
| ------------------------- | --------------- |
| Server startup            |                 |
| Health endpoint           |                 |
| User creation             |                 |
| Channel membership        |                 |
| Message creation          |                 |
| Message history           |                 |
| TCP interface             |                 |
| Shared TCP and HTTP state |                 |
| Concurrent clients        |                 |
| Failure detection         |                 |