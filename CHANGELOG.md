# Changelog

All notable changes to motto-mcp-server will be documented in this file.

## [0.2.0](https://github.com/lkmotto/motto-mcp-server/compare/v0.1.0...v0.2.0) (2026-06-30)


### Features

* add bandit security scanning to pre-commit and CI, configure semantic-release, add release workflow for Northflank deploy ([a6e2a5d](https://github.com/lkmotto/motto-mcp-server/commit/a6e2a5d5237c12eb490306e72928b68617d2ebd9))
* add CodeQL code scanning workflow ([8b4f51e](https://github.com/lkmotto/motto-mcp-server/commit/8b4f51e0b1e08caedf80f581a9280f21a69bb670))
* add fleet proxy tools — pipeline + cockpit via private Northflank DNS ([9abb091](https://github.com/lkmotto/motto-mcp-server/commit/9abb0910b19d2f6afaf1616cb43aa142bb4365a0))
* add grabber MCP server (enqueue/list/get/cancel rotations, health, playbooks) ([#11](https://github.com/lkmotto/motto-mcp-server/issues/11)) ([a59d40f](https://github.com/lkmotto/motto-mcp-server/commit/a59d40f4c7319a63952fd3a4b6d24bf66fc74713))
* add HTTP long-polling to local-task endpoints ([#20](https://github.com/lkmotto/motto-mcp-server/issues/20)) ([e1c462b](https://github.com/lkmotto/motto-mcp-server/commit/e1c462be920fa0dc4bfc7d72a4200962b916c475))
* add MOTTO_MCP_MOUNT_DOMAIN_SERVERS env var to enable grabber MCP tools ([1d0b223](https://github.com/lkmotto/motto-mcp-server/commit/1d0b22312c86a274ab09670e12a786867eaac123))
* add Sentry alert rule configuration script ([772fe4b](https://github.com/lkmotto/motto-mcp-server/commit/772fe4bb44133405bb385a13c414e49193285577))
* **ci:** add auto-merge action with meta-PR guard ([#12](https://github.com/lkmotto/motto-mcp-server/issues/12)) ([f0f21c2](https://github.com/lkmotto/motto-mcp-server/commit/f0f21c2d0afed18d19a5b30267a1d138a089da27))
* **cockpit:** /cockpit/director approval queue UI ([#27](https://github.com/lkmotto/motto-mcp-server/issues/27)) ([ee2bd99](https://github.com/lkmotto/motto-mcp-server/commit/ee2bd9993deb4e9ec80c03815f95cbd2ea2614d1))
* **cockpit:** adaptive mobile layout (\u2264640px + \u2264380px) ([#26](https://github.com/lkmotto/motto-mcp-server/issues/26)) ([b12b636](https://github.com/lkmotto/motto-mcp-server/commit/b12b6365e92085b46d40ae4dceac443dc51221e0))
* **cockpit:** centralized control UI with director chat + intent submission ([#17](https://github.com/lkmotto/motto-mcp-server/issues/17)) ([26f214f](https://github.com/lkmotto/motto-mcp-server/commit/26f214f1ed73b99f67e855bc7755824521d0cd09))
* **cockpit:** director chat tool-calling — read state + propose moves ([#39](https://github.com/lkmotto/motto-mcp-server/issues/39)) ([8c7d939](https://github.com/lkmotto/motto-mcp-server/commit/8c7d939e569522c1f62c3cc3a37d38a523204121))
* **cockpit:** migrate /cockpit/chat to DeepSeek V4-Flash (mirrors director PR [#36](https://github.com/lkmotto/motto-mcp-server/issues/36)) ([#25](https://github.com/lkmotto/motto-mcp-server/issues/25)) ([cd78a1f](https://github.com/lkmotto/motto-mcp-server/commit/cd78a1fcf83954ccc334f0aece7452ecb497803a))
* **local-bridge:** /local/runner-heartbeat HTTP shim ([#19](https://github.com/lkmotto/motto-mcp-server/issues/19)) ([994a540](https://github.com/lkmotto/motto-mcp-server/commit/994a540535f4f2033a77a2c1d1d5f21afdc94a8f))
* **local-bridge:** task queue + HTTP endpoints + cockpit panel ([#18](https://github.com/lkmotto/motto-mcp-server/issues/18)) ([88e04f4](https://github.com/lkmotto/motto-mcp-server/commit/88e04f4abbbf0bf14450e186718464d3d5137f0d))
* **mcp:** claim_next_step + release_claimed_step tools for droid autonomy ([#63](https://github.com/lkmotto/motto-mcp-server/issues/63)) ([5cc7ff8](https://github.com/lkmotto/motto-mcp-server/commit/5cc7ff82e60b2b46f25dfc53bf500d49f87c53f1))
* **mcp:** record_artifact_content + artifacts_pending_review + mark_artifact_reviewed ([#28](https://github.com/lkmotto/motto-mcp-server/issues/28)) ([7ea9073](https://github.com/lkmotto/motto-mcp-server/commit/7ea9073785a3c1e42bba9eabe94cedec6defd89c))
* **mcp:** verify_move framework + capability requests + trust scores ([#37](https://github.com/lkmotto/motto-mcp-server/issues/37)) ([281895a](https://github.com/lkmotto/motto-mcp-server/commit/281895a547ab8238569b839c58af8d1623b5845d))
* protect /mcp with StaticTokenVerifier using MOTTO_MCP_AUTH_TOKEN ([7ca0cae](https://github.com/lkmotto/motto-mcp-server/commit/7ca0cae8277145ee0b6aabf3cfad7f8bb8a63e95))
* scaffold FastMCP fleet-coordination server backed by Neon ([#5](https://github.com/lkmotto/motto-mcp-server/issues/5)) ([5fa5221](https://github.com/lkmotto/motto-mcp-server/commit/5fa5221f574780d374abbe79e2c240c0a3ae80e9))
* **servers/doppler:** MCP server for canonical secret management (closes [#3](https://github.com/lkmotto/motto-mcp-server/issues/3)) ([#6](https://github.com/lkmotto/motto-mcp-server/issues/6)) ([3c50c5d](https://github.com/lkmotto/motto-mcp-server/commit/3c50c5d0727a3d62c18cbb5424f81ea7c2d490d8))


### Bug Fixes

* **cockpit/chat:** assistant turn with tool_calls needs content=null ([#40](https://github.com/lkmotto/motto-mcp-server/issues/40)) ([decdfde](https://github.com/lkmotto/motto-mcp-server/commit/decdfde2e5747c760dfed5019b7867e9f3043cd4))
* **cockpit:** pass --dangerously-skip-permissions + IS_SANDBOX=1 for root container ([#24](https://github.com/lkmotto/motto-mcp-server/issues/24)) ([0852b03](https://github.com/lkmotto/motto-mcp-server/commit/0852b038eec308d1b93b88b9f0b552b4a75f1db4))
* **cockpit:** pin claude CLI subprocess stdin to DEVNULL ([#23](https://github.com/lkmotto/motto-mcp-server/issues/23)) ([dd29b36](https://github.com/lkmotto/motto-mcp-server/commit/dd29b36fb346e8861bab9bd64d03a4eca4e2b6a3))
* **db:** fall back to NEON_DATABASE_URL when DATABASE_URL is unset ([#15](https://github.com/lkmotto/motto-mcp-server/issues/15)) ([706f8e8](https://github.com/lkmotto/motto-mcp-server/commit/706f8e864ae50eb118a87b0b2d9d87d466d904ca))
* **docker:** include servers/ directory in image so domain MCP mount works ([#13](https://github.com/lkmotto/motto-mcp-server/issues/13)) ([ec37a0c](https://github.com/lkmotto/motto-mcp-server/commit/ec37a0c2e5ed6032c000a60e0164154b9e13fe99))
* **mcp:** break circular import in verifiers/ — extract types module ([#38](https://github.com/lkmotto/motto-mcp-server/issues/38)) ([2b62a92](https://github.com/lkmotto/motto-mcp-server/commit/2b62a92fc0f41f12829d72619836cb1f118144c5))
* **mcp:** use FastMCP 2.10+ mount(server, namespace=) signature ([#14](https://github.com/lkmotto/motto-mcp-server/issues/14)) ([2a420e1](https://github.com/lkmotto/motto-mcp-server/commit/2a420e1c16cea558db6bd4ae8fc9b364b9298abc))
* **migration:** grabber_jobs.audit_decision_id should be bigint, not uuid ([#16](https://github.com/lkmotto/motto-mcp-server/issues/16)) ([01e6537](https://github.com/lkmotto/motto-mcp-server/commit/01e653725e6377c97c3845f4ec1149f4e2911b71))
* remove FastMCP auth=_mcp_auth() to fix Perplexity connector tools/list ([5950e33](https://github.com/lkmotto/motto-mcp-server/commit/5950e33b67bdfd4be35d7e7b60e4344d675a4840))
* resolve mypy typecheck errors and auto-fix lint ([858ff50](https://github.com/lkmotto/motto-mcp-server/commit/858ff501a5a3364f1520a621a576b7af78b6f08d))
* restore FastMCP 3.3.1 startup compatibility ([cd75a97](https://github.com/lkmotto/motto-mcp-server/commit/cd75a978c46fe7e39c3379dc3b2ddd55d3abccae))
* **server:** validate run_id UUID in record_run_end, return clear error to agent ([49170c9](https://github.com/lkmotto/motto-mcp-server/commit/49170c9beb59193081f76830cecf00bf5aee6a9f))
* standardize pre-commit test hook to uv run pytest ([6d512a3](https://github.com/lkmotto/motto-mcp-server/commit/6d512a34cf93ea75d44cd749851c25e50ba6d9c1))
* update tunnel URL discovery to use metrics endpoint; start server from start_tunnel.cmd ([b389a24](https://github.com/lkmotto/motto-mcp-server/commit/b389a245257ae5bf63a91d2aada2cdb3bfa12267))


### Documentation

* deploy and Perplexity custom-connector registration runbook ([#10](https://github.com/lkmotto/motto-mcp-server/issues/10)) ([0fb1e0c](https://github.com/lkmotto/motto-mcp-server/commit/0fb1e0c17b31886e8fa5b2a9030b358038179c55))


### Code Refactoring

* split cockpit.py into 5 focused modules ([ed29903](https://github.com/lkmotto/motto-mcp-server/commit/ed29903b3f0635d90299da4e0ed86e57f8784c70))

## [0.1.0] — Unreleased

### Added
- Initial release of motto-mcp-server: FastMCP fleet-coordination server backed by Neon Postgres.
- Agent registration, heartbeat, run lifecycle, event emission, and cross-agent intent signals.
- HTTP dashboard at `/dashboard` and JSON fleet status at `/fleet/status.json`.
- Interactive cockpit UI at `/cockpit` with chat and intent submission.
- Fleet proxy tools for pipeline and cockpit via private Northflank DNS.
- Credential rotation job queue (grabber MCP server).
- Doppler MCP server for secret access.
- Idempotent SQL migrations applied automatically on startup.
- Multi-server architecture: fleet server, grabber, and doppler MCPs.
