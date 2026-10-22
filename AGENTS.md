# Workspace access preference

- Use the patch service (`apply_patch`) as the default method for workspace file edits in this project.
- Do not treat a Windows `CreateProcessWithLogonW` error 1385 as proof that patch-based edits are unavailable; try the patch service directly.
- For reading files or running tests, use an available read or execution tool and report when those checks cannot run.
