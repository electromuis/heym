# Plugin API 2

Plugin API 2 adds options to Heym's plugin manifest and handler interface. A plugin can then have its own palette section,
coloured nodes with several exits, richer forms, its own credential types, options loaded at run time, triggers that hear
events, nodes that pause a run until something outside answers, and nodes that loop like the Loop block.

Every new key is optional. A manifest without `"pluginApi": 2` is an API 1 manifest and behaves exactly as before.

## Compatibility rules

1. `pluginApi` is a whole number. Missing means `1`. This Heym supports `1` and `2`. A manifest that asks for a higher
   version is refused at install time: `plugin.json: pluginApi 3 is newer than this Heym supports (2)`.
2. An API 1 manifest is read as today: unknown keys are ignored, the handler gets `ctx` as a plain dict, a node has one
   input and one exit. Installed plugins and saved workflows keep working after an upgrade.
3. An API 2 manifest is strict: an unknown key is an install error naming the place and the key
   (`plugin.json: nodes[2].fields[0]: unknown key "lable"`). Error texts never quote a value.
4. The new keys below are accepted only in an API 2 manifest. An API 1 manifest that uses one keeps today's behaviour
   (the key is ignored), so a plugin can't change meaning by accident.
5. Handler functions are called with keyword arguments, as today. API 2 adds arguments only when the function accepts them
   (a function without `**kwargs` and without the new name is called without it).

## The manifest

```json
{
  "pluginApi": 2,
  "id": "acme",
  "name": "Acme",
  "version": "1.0.0",
  "entry": "handler.py",
  "palette": {
    "section": "Acme",
    "position": "top",
    "color": "#7c3aed",
    "groups": [
      {"key": "people", "label": "People", "color": "#0ea5e9"},
      {"key": "runs", "label": "Runs"}
    ]
  },
  "credentials": [ ... ],
  "nodes": [ ... ]
}
```

### `palette` (package)

| Key | Type | Default | Meaning |
|---|---|---|---|
| `section` | text | none | The palette section the package's nodes go in. Without it they stay under "Plugins". |
| `position` | `top`, `bottom` | `bottom` | Where the section sits among Heym's own sections. |
| `color` | `#rrggbb` | the kind's colour | The colour of every node of the package, unless a group or a node says otherwise. |
| `icon` | file in the package | `icon.svg` | The section's icon. |
| `groups` | list | none | Sub-headings in the section, in this order: `key` (`^[a-z][a-z0-9_-]*$`), `label`, `color` (optional). |

Nodes without a `group`, or in a section without groups, come after the groups.

### A node

New keys next to today's `key`, `name`, `kind`, `function`, `description`, `icon`, `fields`, `dslHint`, `docSlug`:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `group` | group key | none | The palette group it is listed under. An unknown group is an install error. |
| `color` | `#rrggbb` | group, then package, then the kind's colour | The node's colour on the canvas and in the palette. |
| `subtitle` | text | none | A second line on the node face. `{fieldKey}` is replaced by that field's value as set in the form (an option's label for a `select`), e.g. `"as {agent}"`. A value that is an expression shows as `ƒ`. |
| `exits` | list | one unnamed exit | Named outputs, each a handle on the node like Switch's: `key` (`^[A-Za-z][A-Za-z0-9_]*$`), `label`, `color` (optional). See "Exits". |
| `exitsFrom` | `{field, column}` | none | More exits, one per distinct value of a `list` field's column. See "Exits". |
| `inputs` | list | one unnamed input | Named inputs, each a handle like Merge's: `key`, `label`. See "Inputs". |
| `sample` | JSON object | none | An example of what the node gives back. The expression dialog and autocomplete use it before the node has run. |
| `source` | `push`, `loop` | none | Triggers only: how the trigger hears events. See "Triggers". |
| `loop` | `true` | `false` | Actions only: the node loops like the Loop block. See "Loop nodes". |

### Exits

A node with `exits` has one handle per exit, named `exit-<key>` in the saved workflow. Its handler picks the exit with an `exit`
key in the result:

```python
def run(inputs, config, ctx):
    if not config["agent"]:
        return {"exit": "nobody"}
    return {"exit": "started", "runId": "r-42"}
```

- A result without `exit` takes the first exit.
- An `exit` the node doesn't declare fails the node: `Plugin acme node runAgent returned an unknown exit; declared: started, busy`.
- `exit` stays in the output, as Switch keeps `branch`.
- Branches work as Switch's do: blocks reached only through the other exits are skipped; a block reached from the chosen exit and
  from another one still runs. Loop-back edges are respected.
- An error still goes to the node's error handling (retry, Error Handler), not to an exit.
- An edge saved before the node had exits (no `exit-` handle) is not part of any exit and always runs.

**Exits from a field.** `exitsFrom` makes one exit per distinct value of a column of a `list` field, in the order they first appear,
after the fixed `exits`:

```json
"exitsFrom": {"field": "rules", "column": "answer"},
"exits": [{"key": "otherwise", "label": "otherwise"}]
```

The face updates as the rows change. Such an exit's handle is `exit-<value>`; the value may be any non-empty text without
line breaks. A connected exit whose value disappears from the rows shows as a broken handle until its edge is removed.

### Inputs

A node with `inputs` has one handle per input, named `input-<key>`. It runs when every connected input has delivered, as Merge does.
The handler's `inputs` is unchanged (outputs by the label of the block before); the new argument `inputs_by_key` maps each input key
to the output that arrived there (missing when nothing is connected).

## Fields

Today's field keys (`key`, `label`, `type`, `required`, `secret`, `default`, `options`, `dynamic`, `expression`) keep their meaning.

### New types

| `type` | Edited as | Value given to the handler |
|---|---|---|
| `text` | a text area (`rows`, default 4) | text |
| `code` | a code editor (`language`: `python`, `javascript`, `json`, `sql`, `markdown`, `text`) | text |
| `json` | a JSON editor, checked on save | the parsed JSON |
| `expression` | an expression input; the value is always evaluated | the result |
| `keyValue` | rows of name and value | an object of text |
| `multiselect` | checkboxes, or a searchable list when `searchable` | a list of values |
| `credential` | a pick-list of the user's credentials of `credentialType` (a type this package declares) | the credential's values, decrypted |
| `condition` | a yes/no expression in the Condition block's language, with its editor | the text, not evaluated: the handler checks it with `ctx.check` |
| `list` | rows of sub-fields (`fields`; types `string`, `number`, `boolean`, `select`, `text`, `expression`, `condition`) | a list of objects |

Before the handler runs, a value missing from the saved form takes its field's `default` (API 1 leaves it out), a field hidden by
`showIf` is left out, and a `$`-expression is resolved as today, also inside `list` rows, `keyValue` values and `json` strings.
A `condition` is left as text on purpose: a block that checks rules in order checks only the rows it reaches, so a later row that
would fail on this input (a division by zero, a call on a missing value) does no harm. A missing key
reads as `null`, as everywhere in Heym.

### New keys per field

| Key | Applies to | Meaning |
|---|---|---|
| `help` | all | A line under the field. |
| `placeholder` | text-like | Shown when empty. |
| `advanced` | all | The field sits in a collapsed "Advanced" part of the form. |
| `section` | all | A heading the field is listed under; fields with the same section are kept together, in manifest order. |
| `showIf` | all | `{"otherField": value}` or `{"otherField": [values]}`: the field shows only then; all pairs must hold. A hidden field's value is not sent to the handler. |
| `allowExpression` | `select`, `multiselect` | Besides the options, the user may type an expression. |
| `searchable` | `select`, `multiselect` | The pick-list has a search box. |
| `loadOptions` | `select`, `multiselect` | `true` or a function name: the options come from the handler (below). `options` are shown until they load. |
| `rows` | `text` | Height of the text area. |
| `language` | `code` | Highlighting. |
| `credentialType` | `credential` | Which of the package's credential types to offer. |
| `fields` | `list` | The row's sub-fields. |

### Loaded options

```python
def options_agent(config, ctx):            # or the function `loadOptions` names
    return [{"label": "Penny", "value": "penny"}]   # or a list of texts
```

The form calls `POST /api/plugins/{pluginId}/options` with `{"node": "<node key>", "field": "<field key>", "config": {...}}` when it
opens, and again when a field the options depend on changes (`loadOptions` reruns on any change of a `credential` field). `config` holds
the form's current values, with `credential` fields resolved for the signed-in user. A failing function shows its error under the field
and keeps the static `options`. The call has a time limit of 10 seconds.

## Credential types

A package declares credential types; they appear in Heym's Credentials page next to Heym's own:

```json
"credentials": [
  {
    "key": "hub",
    "name": "Acme hub",
    "description": "Where your Acme hub is and its key.",
    "fields": [
      {"key": "url", "label": "Hub address", "type": "string", "required": true, "placeholder": "http://hub:8793"},
      {"key": "token", "label": "API key", "type": "string", "secret": true, "required": true}
    ],
    "test": "test_hub"
  }
]
```

- Fields use the types `string`, `number`, `boolean`, `select`, `text`. A `secret` field is masked and never sent back to the browser.
- The values are stored encrypted like any credential, under the credential type `plugin` with the package id and the type key.
  Sharing works as for Heym's own credentials.
- `test` (optional): a function `test_hub(values, ctx)` that returns nothing or raises with a message; the form gets a "Test" button.
- A node's `credential` field offers the user's credentials of that type. When exactly one fits, a new node picks it by itself.
- The handler gets the decrypted values in `config[<field key>]`. They are kept out of run history and traces like other credentials.
- Uninstalling the package keeps the stored credentials; they come back when it is installed again.

## The handler's `ctx`

In an API 2 plugin, `ctx` is an object that still reads like the API 1 dict (`ctx["node_id"]`, `ctx["node_label"]`) and adds:

| Name | Meaning |
|---|---|
| `ctx.node_id`, `ctx.node_label` | As before. |
| `ctx.workflow_id`, `ctx.execution_id` | The run's workflow and execution. |
| `ctx.log(text)` | A line in Heym's log, tagged with the plugin and the node. |
| `ctx.check(condition)` | Evaluates a `condition` text on the node's inputs exactly as a Condition block would (same expression language, same `$` roots); `True` or `False`, raises with the expression's error. |
| `ctx.evaluate(expression)` | The same for any `$`-expression; returns its value. |
| `ctx.wait_token(key)` | Mints (once per key) the token that may resume a wait on `key`; give it to whoever will answer. |
| `ctx.wait(key, timeout=None)` | Pauses the run here. See "Pausing a run". |
| `ctx.resume` | When the node runs again after a wait: the body that resumed it, else `None`. |
| `ctx.loop` | In a loop node: `{"index", "state"}` of this pass (see "Loop nodes"); else `None`. |

## Triggers

A trigger node with `source` hears events by itself. Without `source` it behaves as in API 1 (the first step of a run started
another way).

### `source: "push"`: a URL per trigger node

Each trigger node gets its own address, shown on its form with a copy button:

```
POST /api/plugins/{pluginId}/hooks/{hookId}
```

`hookId` is made when the node is placed and kept in the node's data. It is an address, not a secret: the plugin decides who may send.

```python
def receive(request, config, ctx):
    """request: method, headers (lower case), query, body (bytes), json (parsed or None)."""
    if request["headers"].get("x-acme-key") != config["hub"]["token"]:
        raise PermissionError("bad key")          # 401
    event = request["json"]
    if event.get("channel") not in config["channels"]:
        return None                                 # 204, no run
    return event                                    # or a list of events: one run each
```

- `receive` is required for a push trigger (`receive_<node key>` is used first when the package has several).
- The workflow must be active and the trigger node enabled. A hook of a deleted or inactive workflow answers 404.
- The answer is `202` with `{"started": <number of runs>}` once the runs are started (the sender doesn't wait for them), `204`
  when no run started, `401` when `receive` raised `PermissionError`, `400` for another error, `413` above 1 MiB, `504` after
  10 seconds.

### `source: "loop"`: a background listener

```python
def listen(config, emit, ctx):
    for event in acme_stream(config["hub"]):     # blocks; return when ctx.stopping is set
        if ctx.stopping.is_set():
            return
        emit(event)
```

- One listener runs per active trigger node of that kind, on the leader instance (as the IMAP trigger polls), in a thread. A
  node is active unless its `active` is false. Heym looks for new, changed and removed nodes every 15 seconds.
- It is stopped (`ctx.stopping` set) when its node is deactivated, changed or deleted, when the package is disabled, uninstalled
  or updated, or when plugins are turned off; a changed node gets a new listener. A listener that returns or raises is started again after 5, 10, 20 ... up to 300 seconds;
  the error is in the logs.
- `emit(event)` starts one run with that event (subject to the filter).

### What every source shares

- The event becomes the trigger node's output. When the manifest node names a `function`, it is called as
  `function(config=..., ctx=..., event=...)` and its result is the output instead. (A trigger with a `source` has no default
  function; one without keeps API 1's default `trigger`.)
- Every trigger node with a `source` has a field "Only start when" (an expression over `$event`, e.g.
  `$event.channel == "general"`), checked before a run starts. False, or an error in it, starts no run; an error is logged.
- A trigger with `exits` picks one with the event's `exit` key, as an action does.

## Pausing a run

```python
def run(inputs, config, ctx):
    if ctx.resume is not None:                     # we were woken
        return ctx.resume                          # its `exit` picks the exit
    token = ctx.wait_token("run-42")
    start_remote_job(callback_token=token)
    ctx.wait("run-42", timeout=3600)               # pauses here; never returns
```

- `ctx.wait` pauses the run like a review request does: the run's status is "pending", it is in the run history, and it
  survives a restart. While it waits, the node's output is `{"status": "waiting", "key": ..., "expiresAt": ...}`.
- When the run resumes, the handler is called again from the start, with the same inputs and config and `ctx.resume` set. Work done
  before `ctx.wait` is done again unless the handler checks `ctx.resume` first, as above. `ctx.resume` is set for that one call only.
- `ctx.wait` raises an exception that doesn't derive from `Exception`, so a handler's `except Exception:` doesn't swallow it.
- `POST /api/plugins/{pluginId}/waits/{key}` resumes it. It takes the token from `X-Heym-Wait-Token` or a signed-in user who may open
  the workflow. The body must be a JSON object (empty means `{}`, at most 1 MB). It becomes `ctx.resume` when the node runs again, and
  its `exit` picks the exit. The answer is `202` once the run is resuming (the sender doesn't wait for it), `404` when nothing ever
  waited on that key, `401` without a valid token or user, `409` when the wait already ended, or `422` for a body that isn't a JSON
  object.
- `timeout` in seconds: when it passes, the node runs again with `ctx.resume = {"exit": "timedOut"}`. Timeouts are checked every 30
  seconds, so one may end up to 30 seconds late. A node that waits with a timeout must declare a `timedOut` exit, or it fails.
  Without a timeout the run waits until it is resumed or stopped.
- A key is one wait at a time per package. Waiting on a key that already waits fails the node. Only the token's hash is stored; a
  wait without a `ctx.wait_token` can only be resumed by a signed-in user.
- Stopping the run ends the wait.
- Inside an Execute block's sub-workflow a wait is refused, as a review request is.
- Stored in the table `plugin_waits` (`app/services/plugin_waits.py`); a resumed run is written to its history row as a resumed
  review is.

## Loop nodes

An action with `"loop": true` loops like the Loop block, but it gets its next item while it runs instead of from a list fixed
at the start. A pass may wait for something outside (`ctx.wait`), so a loop over the steps of a long job holds no thread while
it waits and goes on after a restart.

```json
{"key": "eachStep", "name": "Each step", "kind": "action", "function": "each_step", "loop": true}
```

```python
def each_step(inputs, config, ctx):
    job = ctx.loop["state"].get("job") or start_job(config)
    if ctx.resume is None:
        ctx.wait_token(f"{job}-{ctx.loop['index']}")      # give it to the job
        ctx.wait(f"{job}-{ctx.loop['index']}", timeout=3600)
    step = ctx.resume
    if step.get("exit") == "timedOut" or step.get("last"):
        return {"exit": "done", "job": job}
    return {"exit": "step", "step": step, "state": {"job": job}}


def loop_end(state, ctx):                                   # the run stopped before the loop said done
    cancel_job(state.get("job"))
```

- The node has Loop's handles: the exits `step` (handle `exit-step`, the body hangs from it) and `done` (`exit-done`), and an
  input `loop` for the edge back from the end of the body. An edge into `loop` is a loop-back edge, as it is for the Loop block.
  Such a node declares no `exits`, `exitsFrom` or `inputs`; doing so is an install error.
- A pass is one call of the handler. `ctx.loop` is `{"index": <pass, from 0>, "state": <object>}`: `state` is what the previous
  pass returned under `state`, `{}` on the first pass. On later passes `inputs` also holds the output of the block whose edge came
  back into `loop`.
- The result's `exit` says what comes next: `step` (also when `exit` is missing) runs the body with the result as the node's
  output, and the edge back into `loop` starts the next pass; `done` leaves the loop. Any other `exit` fails the node:
  `Plugin loop node eachStep returned an unknown exit; declared: step, done`.
- `state` must be a JSON object. It is kept with the run (and with a paused run's snapshot) and left out of the output.
- The output of `done` is that pass's result. The passes' outputs are not collected, as Loop's `results` would grow with every
  step.
- A pass may `ctx.wait` as any node may. When it resumes, the same pass runs again from the start with the same `ctx.loop` and
  `ctx.resume` set. A timeout gives `ctx.resume = {"exit": "timedOut"}`; the handler still answers `step` or `done`, so a loop node
  needs no `timedOut` exit.
- Nested in a Loop block (or another loop node), the loop starts over on the outer loop's next item. A Loop block may sit in the
  body of a loop node. Lead the inner loop's `done` through a block before the edge back into the outer loop, as with two Loop
  blocks.
- When the run ends while the loop is open (it is stopped, fails or times out, also while it waits), the package's
  `loop_end_<node key>(state, ctx)` or else `loop_end(state, ctx)` is called, when it has one, with the last pass's `state`.
  `ctx` has `node_id`, `node_label`, `workflow_id`, `execution_id` and `log`. An error in `loop_end` is logged; the run has ended
  either way. A loop that said `done` gets no `loop_end`.
- An API 1 manifest with `"loop"` keeps today's behaviour: the key is ignored and the node runs once.

## Worked example: a Rules block

Ordered yes/no rules about different facts, where the first that holds wins, are a chain of Condition blocks in stock Heym, and
Switch can't hold them (it compares one value with fixed cases). With API 2 they are one plugin node:

```json
{
  "key": "rules", "name": "Rules", "kind": "action", "function": "rules", "subtitle": "first rule that holds",
  "fields": [
    {"key": "rules", "label": "Rules", "type": "list", "fields": [
      {"key": "name", "label": "Name", "type": "string", "required": true},
      {"key": "when", "label": "When", "type": "condition", "required": true},
      {"key": "answer", "label": "Answer", "type": "string", "required": true}
    ]},
    {"key": "otherwise", "label": "Otherwise", "type": "string", "default": "otherwise"}
  ],
  "exitsFrom": {"field": "rules", "column": "answer"},
  "exits": [{"key": "otherwise", "label": "otherwise"}]
}
```

```python
def rules(inputs, config, ctx):
    for row in config["rules"]:
        if ctx.check(row["when"]):
            return {"exit": row["answer"], "answer": row["answer"], "rule": row["name"]}
    return {"exit": "otherwise", "answer": config["otherwise"], "rule": None}
```

The output's `rule` says in the run history which row held. Several rows may share an answer; they share its exit.

## Listing (`GET /api/plugins`)

Each package gains `pluginApi`, `palette` and `credentials` (without secrets); each node gains `group`, `color`, `subtitle`, `exits`,
`exitsFrom`, `inputs`, `sample`, `source` and `loop` (a loop node lists its exits `step` and `done`). API 1 packages list them as
`null`.

## What API 2 leaves out

Async handlers and a time limit per node; inputs that start a node when any one delivers; plugins loaded from a folder at start-up;
migrating a node's saved config when its fields change.
