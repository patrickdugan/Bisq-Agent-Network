# Cloud pod deployment

Bisq Agent Network is a small Python CPU-only sidecar. These deployments leave fund movement and trading disabled.

## A. Ephemeral cloud Docker pod: automated in GitHub Actions

The repository's Cloud Pod Smoke workflow runs on a GitHub-hosted Linux VM. It builds cloud/Dockerfile, starts the BAN dashboard container on 127.0.0.1:8088, starts a SYNTHETIC Bisq 2 REST mock on 127.0.0.1:8090 and tests the network endpoints, quote math and trade-denial boundary.

The smoke and node contract reports are uploaded as JSON in the run's artifact named ban-cloud-pod-report. The workflow has no live Bisq credentials and does not connect to a real Bitcoin trading network.

The container is stopped and removed when the workflow finishes. The runner is temporary, and the dashboard has no publicly accessible address. You can trigger the job again from GitHub Actions > Cloud Pod Smoke > Run workflow.

## B. Persistent Linux VPS or pod host

Use cloud/compose.yaml on a Linux host with Docker Engine and Compose, plus a separately deployed and configured Bisq 2 node on the SAME host bound to 127.0.0.1:8090.

IMPORTANT: the upstream headless Bisq 2 api-app ships with REST disabled and WebSocket enabled by default. You must enable REST in a reviewed node configuration. The upstream config field is application.api.server.restEnabled. Pair a read-only client using Bisq itself. Never reveal pairing codes, client secrets, or trading permissions to this application or in bug reports.

From a persistent Linux host:

    git clone https://github.com/patrickdugan/Bisq-Agent-Network.git
    cd Bisq-Agent-Network

Create cloud/.env on the host with the read-only client ID and current session ID, then chmod 600 cloud/.env. The .env file is ignored by Git. Never copy its contents into an issue or chat.

    BAN_BISQ_CLIENT_ID=YOUR_PAIRED_READONLY_CLIENT_ID
    BAN_BISQ_SESSION_ID=YOUR_CURRENT_SESSION_ID

Start and verify:

    docker compose --env-file cloud/.env -f cloud/compose.yaml up -d --build
    docker compose --env-file cloud/.env -f cloud/compose.yaml exec ban ban --source bisq2 node-check --currency PHP

Use SSH forwarding for operator access:

    ssh -N -L 8088:127.0.0.1:8088 YOUR_USER@YOUR_HOST

Open http://127.0.0.1:8088 in your laptop browser. Keep port 8090 and 8088 private. The dashboard has no public user authentication. The container uses host networking intentionally so it can communicate with an upstream REST API that binds only to the Linux host's loopback address.

The Bisq pairing session expires. Update the local secret and restart the container after authorized re-pairing if necessary.

The persistent SQLite cohort ledger is kept in a Docker named volume called ban_data. It is sensitive to merchant consent windows and withdrawal requests. Encrypt server storage, limit admin access, and manage backups accordingly.

The quote reference still has no verified upstream oracle timestamp. Real Bisq 2 nodes and trades must be validated separately. The cloud workflow only verifies the software with synthetic API responses. No platform-specific pod was rented or provisioned by GitHub Actions.
