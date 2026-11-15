# Roadmap and engineering gates

## Milestone 1 — inventory and SMART
- List devices returned by smartmontools.
- Display model, serial, capacity, temperature, power-on hours and available SMART attributes.
- No write-capable disk operation exists in this milestone.

## Milestone 2 — read-only surface scan
- Open physical devices read-only.
- Use bounded sequential reads and conservative timeouts.
- Record LBA ranges, latency and read errors.
- Never label slow sectors as confirmed bad sectors.
- Provide cancel and resumable scan metadata.
- Warn users that scanning a failing disk can accelerate failure.

## Milestone 3 — imaging
- Source opened read-only; destination must be a different physical device or image file.
- Verify source/destination identity and capacity before starting.
- Copy easy regions first; retry difficult regions with strict limits.
- Persist map/log after each chunk and verify source identity before resuming.
- Never overwrite an existing destination without explicit confirmation.

## Milestone 4 — recovery
- Work from an image where possible.
- Save recovered files to a separate destination.
- Implement filesystem-aware recovery before file carving.
- Report confidence and metadata loss.

## Milestone 5 — maintenance
- No generic "repair bad sectors" button.
- Expose only documented vendor/OS operations with clear write warnings.
- Separate logical filesystem repair from physical media failure.

## Windows PE
Keep the core engine independent from the GUI. Validate against multiple PE builds and storage drivers. A PE build is a separate packaging target, not assumed compatible merely because the Windows desktop EXE runs.
