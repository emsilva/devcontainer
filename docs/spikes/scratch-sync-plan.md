# Scratch sync spike

Approved scope: test automatic, bidirectional `.scratch/` synchronization between
the Mac and an existing Codespace. This is a feasibility experiment,
not a wizard rollout or a determination of company policy.

## Boundaries

- Use synthetic files in new, uniquely named directories only.
- Leave the repository, agent instructions, existing SSH configuration, login
  services, and normal Mutagen state unchanged.
- Download the official macOS arm64 Mutagen release into the private experiment
  directory and verify its vendor SHA256 checksum.
- Use a separate Mutagen data directory, no global configuration, and a dedicated
  OpenSSH configuration. Retrieve the work-account token per process; never write
  it to files, arguments, reports, or logs.
- Start the existing stopped Codespace explicitly for the experiment. A transport
  guard must refuse connections when the Codespace is not Available. Test network
  interruption without stopping a machine that someone may be using.
- Terminate the test session and its isolated daemon before removing test data.
  Remove only remote agent files proven to have been created by this experiment.

## Execution and evidence

- [x] Establish clean local and remote repository baselines and record paths.
- [x] Verify that `git add --all` stages the scratch placeholder instructions but
      leaves nested working files untracked and ignored.
- [x] Create a `two-way-safe` session and measure ordinary watched changes from
      Mac to Codespace and Codespace to Mac without manual flush commands.
- [x] Verify nested paths, spaces/Unicode names, binary hashes, edits, renames,
      and ordinary file deletions.
- [x] Pause synchronization, change the same file differently at both endpoints,
      resume, and verify that both contents survive and a conflict is visible.
- [x] Interrupt the transport, create nonconflicting offline changes on both ends,
      restore it, and verify automatic catch-up.
- [x] Exercise the connection guard with a non-running state and verify that it
      never invokes the SSH/start command.
- [x] Verify cleanup, unchanged repository baselines, and record limitations:
      physical Mac sleep, full container rebuild, Codespace replacement, idle
      timeout effects, and recoverable history are not established by this trial.
- [x] Write the measured result, source evidence, integration recommendation, and
      remaining gates; run `task check` and commit the report on the spike branch.
