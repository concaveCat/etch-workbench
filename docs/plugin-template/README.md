# Plugin template

A minimal, complete process-plugin package for the v2 contract. It is a
template, not an installed plugin: it deliberately lives under `docs/` so the
loader's `plugins/*/plugin.toml` scan never discovers it.

## Copy and install

1. Copy this directory to `plugins/<id>/` and rename it to the plugin id.
2. Update `plugin.toml` (`id`, `version`, and `entrypoint`, for example
   `plugins.<id>:register`), then replace the placeholder params, spec, step
   parser, and variant with real code.
3. Regenerate and verify the lock from the repository root:
   ```bash
   .venv/bin/python scripts/lock-plugins.py
   .venv/bin/python scripts/lock-plugins.py --check
   ```
4. Restart the worker/service. Discovery and the registry are built at startup;
   there is no hot reload.

`variants/native.py::execute` returns an empty `StateDelta`, so the template
imports and registers but does not run a physics model.

## Contract map

| File | Contract |
| --- | --- |
| `plugin.toml` | Strict manifest: `api = 1`, `id`, `version`, one `module:attr` entrypoint. |
| `spec.py` | `ProcessSpec(default_variant="native", parse_step=parse_step)`. |
| `step.py` | Maps a raw worker step to params, `None` for another process, `ValueError` when malformed. |
| `variants/native.py` | `EngineVariant.execute(state, params, ctx, engine) -> StepResult(delta=StateDelta(...))`. Declare supported `EngineRequirement.profiles` (for example CPU_DISK) and use `apply_execution_profile`; do not hard-code a kernel. The delta is flow state only; `outputs`, `metadata`, and `warnings` are report data. |
| `register.py` | Manifest entrypoint; registers exactly `(process_id, variant)`, and the registry derives ids, capabilities, engine requirement, and implementation hash. |

`VariantCapabilities.state_profile` is optional. Add it only when the variant
needs conversions, required materials, or replayable state; the resolver turns
it into a `StateContract` and fails closed if the engine's `StateBackend`
cannot provide the declared capability.

The implementation hash covers every `.py` file in the installed plugin
package, so editing any module requires a version bump and a regenerated lock.
