# Deniz personal agent: approved hybrid direction

Date: 2026-09-30
Status: User accepted the proposed direction and instructed implementation to continue.

## Product outcome

Deniz is a personal agent available through desktop, Telegram and iMessage. It understands a request, uses appropriate tools, follows work through verification, remembers supported preferences and ongoing responsibilities, and lets the user steer work while it runs. Natural conversation and capabilities are shared across all three channels. Transport and available execution environments can differ.

The user referenced OpenAI Dots and accepted the recommended hybrid architecture. The target is the observable behavior described here; this does not claim integration with Dots or access to its proprietary runtime.

Deployment clarification from the user: there is no VPS; proceed on the current computer now. A currently unused Windows computer may become a continuously available executor later. The first deliveries therefore run locally on the Mac. The executor boundary remains extensible, but no cloud dependency, server purchase or Windows enrollment belongs to the current delivery.

## Existing foundation

The repository already has a model/tool agent, cross-process Mac ownership and user priority, approvals, shared personal memory, task checkpoints, scheduling, companion heartbeat and guarded autonomy. These are reusable components. Existing checkpoints and heartbeat are not a durable distributed task service. Current execution depends on the Mac being available.

## Delivery sequence and boundaries

### 1. Shared conversation, evidence and activity

Implement the independently reviewed [shared core specification](2026-09-30-shared-conversation-core-design.md). Casual conversation has a fast path; current information and local-state questions require actual observations; substantial or effectful work uses the existing agent. Keep exact useful facts in the final answer. Show real activity, preserve selected providers, and deploy the same verified revision to all three installed channels.

This is the first independently deliverable subproject. It creates interfaces for future task orchestration without introducing remote execution or a new infrastructure dependency.

### 2. Durable responsibilities and task control

Design and implement a persistent task service around existing execution. Persist user-scoped task IDs, requested outcomes, origin, authorization scope, dependencies, assigned executor, progress, verified artifacts and delivery state. Separate responsibilities, scheduled triggers and individual runs. Support steering and priority changes while work runs.

Use explicit queued/running/waiting-user/waiting-executor/verifying/completed/failed/cancelled states. Recover expired worker ownership after restart. Effectful work with an uncertain outcome must be inspected before retry; never promise exactly-once external effects. A completed run must retain whether the requested result was verified and delivered. Stopping a run and cancelling its recurring responsibility are separate user actions.

Cross-channel continuation resolves authenticated identity and task ID. Shared context is selected for relevance and permissions; raw conversations are not blindly concatenated. Notifications go to the chosen destination with suppression of duplicates.

This subproject needs its own focused specification, implementation plan and acceptance scenarios after the first delivery.

### 3. Hybrid always-on execution

An always-on coordinator maintains tasks and responsibilities. Cloud workers handle eligible research, documents and repository jobs. A paired Mac executor handles local files, logged-in local applications and desktop actions. Choose the executor by required capabilities, authorized data access and current availability; do not transfer local files, credentials or entire conversations implicitly.

The user's current rollout choice is local-first: the Mac hosts coordination and execution together. A later Windows executor can supply eligible research/document/repository capabilities while online; Mac-only applications, local files not explicitly shared and current iMessage transport still wait for the Mac. Windows support requires its own pairing, capability and outcome-verification design rather than assuming Mac GUI tools are portable.

When the Mac is offline, cloud-capable work can continue. Mac-only work enters waiting-executor and resumes under the same authorization when the Mac returns. Current iMessage transport also depends on the Mac: pending outbound iMessage delivery waits for it. Telegram can become independently reachable only after its receiver is moved to an always-on service.

Executor enrollment uses authenticated pairing, revocable credentials, transport encryption, replay-resistant task leases and no public unauthenticated Mac control endpoint. Remote workers receive only the task context and secrets they need. Existing action approvals apply regardless of executor. The user retains stop, inspect and connection revocation controls.

Before provisioning, prepare a concrete deployment proposal identifying provider, region, monthly cost limit, model usage limits, data retention, secret storage and migration/rollback. Acceptance of the hybrid direction authorizes design and implementation preparation; it does not select a paid provider or authorize unlimited infrastructure spending.

### 4. Additional connected capabilities

Add applications and tools for concrete user workflows, with advertised availability, scoped permissions and outcome verification. Reuse existing integration discovery. Add connectors when a selected workflow requires them; a long connector catalog is not evidence that a task can be completed.

## Success examples

- Current model question: fresh research, supported names/dates/links, honest unknowns.
- Desktop directory question: observed names and useful categories, no invented personal conclusions.
- Project fix: inspect, change, verify, provide reviewable output and relevant remaining limits.
- Ongoing watch: saved scope, trigger and notification condition; list/change/cancel available.
- Mid-task steering: preserve the existing goal, update priority or constraints, expose the real state.
- Cross-channel continuation: resolve the same task and show its verified result without duplicating delivery.
- Mac offline: continue eligible cloud tasks; visibly wait for unavailable local capabilities.

## Sources

- [OpenAI Dots tasks and memory](https://learn.chatgpt.com/docs/dots/tasks-and-memory): responsibilities, background work and steering.
- [OpenAI Dots computers and apps](https://learn.chatgpt.com/docs/dots/computers-and-apps): distinct cloud and connected local environments.
- [OpenAI Dots controls](https://learn.chatgpt.com/docs/dots/controls): action scope, permissions and inspection.

These sources inform the product comparison. OmniAgent's design and acceptance are defined by this repository and the user's requests.
