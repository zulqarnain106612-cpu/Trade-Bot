"""
Universal runtime platform.

One model for every long-lived thing the bot runs -- strategies, models,
engines, tuning parameters, workers, tasks, providers, the event bus -- so a
single place can answer what exists, at which version, in which state, how
healthy, depending on what, able to do what, and whether that is what was
asked for.

Modules:

* ``contracts``    -- identity, immutable versions, capabilities, the lifecycle
                       transition table, desired state, health.
* ``registry``     -- the runtime registry (the bookkeeping of record).
* ``adapters``     -- read the existing specialised registries into specs;
                       none of them is replaced.
* ``supervisor``   -- executes lifecycle actions through per-type controllers,
                       with failure, restart and quarantine policy.
* ``reconcile``    -- desired-versus-actual diff and the reconciler.
* ``dependencies`` -- dependency graph, cycle detection, impact analysis.
* ``changes``      -- the change manager: the only path that mutates runtime
                       state.

Nothing here executes code supplied at runtime: a reload or a replace is a
call into a controller the component's own package already provides, never
``exec``, monkeypatching or function replacement.
"""
