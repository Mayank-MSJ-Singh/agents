# LiveKit Agents - Critical Bugs Investigation & Solution Guide

This document tracks our investigation into four critical issues reported for the LiveKit Agents framework, along with detailed step-by-step guides for how to resolve them.

---

## 1. Issue #6315: Race Condition Between `session.run()` and `AgentTask` Handoffs

### Deep Code Verification (Is this real?)
**YES, this is a real and critical bug.** 
I analyzed `AgentTask.__await_impl()` in `agent.py` and `AgentSession.run()` in `agent_session.py`. 

Here is exactly why it happens:
1. `AgentSession.run()` is a **synchronous** method that users can call at any time to generate a reply. When called, it immediately sets `self._global_run_state = RunResult(...)`.
2. Meanwhile, when an `AgentTask` is invoked (e.g., from a tool), it triggers an asynchronous context switch via `await session._update_activity(...)`. 
3. While `AgentTask` is yielding to the event loop, an external event or user script could trigger `session.run()`.
4. After `_update_activity` finishes, `AgentTask` resumes and executes `run_state = session._global_run_state`.
5. **The Fatal Flaw**: Because it fetches the state *after* the `await`, it accidentally grabs the *brand new* `RunResult` created by `session.run()`. 
6. It then attempts to remove its old `speech_handle` from this *new* `RunResult` (which does nothing, because the handle was registered to the *old* `RunResult`). As a result, the old `RunResult` hangs forever waiting for a handle that will never complete, and the new `RunResult` gets corrupted with an `on_enter_task` from a completely different lifecycle.

### The Solution Guide
1. **Cache the state before yielding**: Inside `AgentTask.__await_impl()`, we must capture `original_run_state = session._global_run_state` **before** calling `await session._update_activity()`.
2. **Safe registration**: After waking up from `_update_activity`, we should only perform `_unwatch_handle` and `_watch_handle` operations on `original_run_state`, completely ignoring whatever is currently in `session._global_run_state`. This perfectly isolates the state mutations. 

---

## 2. Issue #6313: Deadlock on Nested `AgentTask` Inside `on_enter()`

### Deep Code Verification (Is this real?)
**YES, this is a real and guaranteed deadlock.**
I inspected the `AgentTask.__await_impl()` code in `agent.py` (specifically lines ~907 to ~954). 

Here is exactly how the deadlock forms:
1. When an agent starts, it spawns an `on_enter_task` to run the `agent.on_enter()` setup hook (Task A).
2. Inside `on_enter()`, the developer triggers a tool call which returns an `AgentTask` and awaits it (Task B). So, Task A is now paused, waiting for Task B to complete.
3. Task B (`AgentTask.__await_impl()`) begins executing. It sees that its parent `on_enter_task` (Task A) is still running. 
4. The code explicitly attempts to protect the session state by doing `await asyncio.shield(on_enter_task)` (either immediately if there is no `run_state`, or inside the `finally` block at the end of the handoff if there is).
5. **The Fatal Flaw**: Task B is now awaiting Task A. But Task A is already awaiting Task B! 
6. This forms a perfect circular wait (Deadlock). The `AgentTask` will block forever waiting for `on_enter()` to finish, but `on_enter()` can't finish until the `AgentTask` returns.

### The Solution Guide
1. **Identify the inline execution context**: In `AgentTask.__await_impl()`, before shielding or tracking `on_enter_task`, we must check if we are actually executing *inside* the `on_enter` flow.
2. **Break the loop**: We can check if `on_enter_task` is identical to `asyncio.current_task()`, or simply check if `on_enter_task` is already in the `blocked_tasks` list (which the framework populates with the current task tree). 
   ```python
   if on_enter_task and on_enter_task not in blocked_tasks:
       # Safe to shield / watch, because we are an external task
   ```
3. By ensuring we only `await on_enter_task` if we are an external task, we break the circular deadlock and allow the inline tool call to finish seamlessly.

---

## 3. Issue #6308: `to_provider_format` Crashes with `JSONDecodeError`

### Deep Code Verification (Is this real?)
**YES, this is a real and highly reproducible bug.**
I audited the translation layers for Anthropic, Google, and AWS (`_provider_format/anthropic.py`, `google.py`, `aws.py`).

Here is exactly how the system crashes:
1. When an agent (LLM) decides to use a tool, it streams a `FunctionCall` message (with the tool name and its arguments as a JSON string).
2. If the human user interrupts the agent (speaks over it) mid-sentence, the framework correctly halts the LLM generation. 
3. However, this means the `msg.arguments` string for that `FunctionCall` is abruptly cut off (e.g., `{"location": "San`). 
4. The framework retains this partial message in the chat history. On the *next* turn, when it tries to send the chat history back to the LLM, it invokes `chat_ctx.to_provider_format()`.
5. The provider formatters blindly execute:
   ```python
   "args": json.loads(msg.arguments or "{}")
   ```
6. **The Fatal Flaw**: Passing truncated strings like `{"location": "San` to `json.loads` natively throws a `json.JSONDecodeError`. Because this isn't caught by the formatter, the exception bubbles up, permanently crashing the chat loop. The agent will completely halt.

### The Solution Guide
1. **Safe JSON Parsing**: In all three affected files (`livekit/agents/llm/_provider_format/google.py`, `aws.py`, `anthropic.py`), we must wrap the `json.loads` calls in a `try...except` block.
2. **Implementation Snippet**:
   ```python
   try:
       parsed_args = json.loads(msg.arguments or "{}")
   except json.JSONDecodeError:
       logger.warning(f"Failed to parse tool arguments for {msg.name}, falling back to empty dict")
       parsed_args = {}
   ```
3. By safely degrading to an empty dictionary `{}`, the LLM sees the historic function call but the session continues without crashing.

---

## 4. Issue #6298: `ClosedResourceError` on MCP Server Death

### Deep Code Verification (Is this real?)
**YES, this is a real unhandled edge case.**
I audited `livekit/agents/llm/mcp.py` within the `_make_function_tool._tool_called` logic.

Here is exactly what happens when an MCP Server dies:
1. The framework maintains a persistent `ClientSession` over `anyio` streams to communicate with the external MCP tool server.
2. When the LLM decides to use an MCP tool, the agent triggers:
   ```python
   tool_result = await self._client.call_tool(name, raw_arguments)
   ```
3. If the external server process unexpectedly crashes or the connection drops exactly while this tool call is being awaited, the underlying `anyio` read stream receives an EOF or pipe closure.
4. `anyio` raises an `anyio.ClosedResourceError` (or `anyio.EndOfStream`) up through the `call_tool` awaitable.
5. **The Fatal Flaw**: Because there is no `try...except` block in `_tool_called`, this low-level socket exception bypasses the framework's intended `ToolError` mechanism. It bubbles up to the main agent loop as a fatal, unhandled `Exception`, which can permanently crash the agent's task executor.

### The Solution Guide
1. **Catch Low-Level Exceptions**: Inside `_tool_called` in `mcp.py`, we need to wrap the `call_tool` operation to catch any network/socket errors.
2. **Implementation Snippet**:
   ```python
   import anyio

   try:
       tool_result = await self._client.call_tool(name, raw_arguments)
   except (anyio.ClosedResourceError, Exception) as e:
       raise ToolError(f"MCP server disconnected or failed during tool invocation: {e}")
   ```
3. By explicitly raising a standard `ToolError`, the LiveKit framework knows how to gracefully handle the failure. It intercepts the `ToolError`, marks the function call output with `is_error=True`, and feeds it back to the LLM. The agent can then recover and say "Sorry, the tool crashed" instead of the whole Python process breaking.

---

## 5. Issue: Resource Leak (`_forward_video_atask`) on Agent Session Close

### Deep Code Verification (Is this real?)
**YES, this is a real and confirmed background task leak.**
I audited `agent_session.py` specifically focusing on the lifecycle of background I/O tasks. 

Here is exactly how the leak occurs:
1. When a user changes their video input, `AgentSession._on_video_input_changed` cancels any existing `_forward_video_atask` and creates a new one to process frames (`self._forward_video_atask = asyncio.create_task(...)`).
2. When the session finally terminates, it calls `_aclose_impl` to gracefully tear down resources and clean up dangling tasks.
3. Inside `_aclose_impl`, it explicitly cleans up the audio task (`await utils.aio.cancel_and_wait(self._forward_audio_atask)`), the `_recorder_io`, and `_ivr_activity`.
4. **The Fatal Flaw**: The teardown sequence completely omits `self._forward_video_atask`.
5. Under typical shutdown flows, if `self._activity` is active, it calls `self.input.video = None` which triggers `_on_video_input_changed()`. This cancels the active forwarding task, but immediately schedules a *new* forwarding task. Although this new task exits quickly because the stream is `None`, it is redundant.
6. More critically, if the session is aborted early (e.g. before an activity is initialized), `self._activity` is `None` and the `self.input.video = None` block is completely skipped. The active `_forward_video_atask` is **never cancelled**, resulting in a permanent resource leak.

### Test Verification
We created a reproducible test suite at `rough_test/test_leak.py` with two distinct test scenarios:
* **Case 1 (Early abort, no activity)**: Proves that `_forward_video_atask` was left running indefinitely, leaking memory and resources.
* **Case 2 (Active activity)**: Proves that even during a normal shutdown, the task was cancelled only as a side-effect, immediately spawning a redundant task.

### The Solution
1. **Cancel the Task in Shutdown**: Inside `AgentSession._aclose_impl()` (around line 1055), add explicit cancellation and awaiting for the video forwarding task exactly like the audio forwarding task.
2. **Implementation**:
   ```python
   if self._forward_audio_atask is not None:
       await utils.aio.cancel_and_wait(self._forward_audio_atask)

   if self._forward_video_atask is not None:
       await utils.aio.cancel_and_wait(self._forward_video_atask)
   ```
3. **Verification Results**: Applying this patch completely resolved the leak in both test scenarios. Running the test suite yields:
   ```
   --- CASE 1: Session starts but no activity is initialized ---
   Video Task started & active: True
   Calling real session.aclose()...
   Video Task is done after close: True
   ✅ Case 1 terminated cleanly.

   --- CASE 2: Session has an active activity ---
   Video Task started & active: True
   Calling real session.aclose()...
   Video Task is done after close: True
   ✅ Case 2 terminated without leaking.
   ```
4. All unit tests (`pytest --unit`), lint checks (`make check`), and type verification checks pass successfully.
