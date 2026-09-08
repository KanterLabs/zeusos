# Zeus OS Helm backlog

Project: [ZeusOS / ZOS](https://tc.shanekanterman.dev/p/zeusos). Updated from the desktop-first implementation and testing checklists on September 8, 2026.

Verified **50 unclaimed Backlog cards**, **186 unchecked acceptance criteria**, and **76 native implementation dependency edges**. The original import task is ZOS-1 and the preview-plan/VM setup task is ZOS-52; none of the planned implementation/testing work is marked started or completed.

Implementation prerequisites are native Helm dependencies. Testing suites span multiple milestones, so their case-level prerequisites and product-capability references remain linked in the descriptions. This lets early cases run before later suites are complete.

## Implementation

| Plan ID | Helm card | Milestone |
| --- | --- | --- |
| I01 | [ZOS-2 — Establish the repository and release contract](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-2) | M0 — Prove the architecture |
| I02 | [ZOS-3 — Select and verify the exact artifact set](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-3) | M0 — Prove the architecture |
| I03 | [ZOS-4 — Prepare an isolated builder and disposable test resources](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-4) | M0 — Prove the architecture |
| I04 | [ZOS-5 — Prove desktop installation and deployment rollback](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-5) | M0 — Prove the architecture |
| I05 | [ZOS-6 — Prove persistent remote T3 and Codex execution](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-6) | M0 — Prove the architecture |
| I06 | [ZOS-7 — Assemble the complete image and core application suite](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-7) | M1 — Build the usable preview image |
| I07 | [ZOS-8 — Productize the safe installer and QCOW2 artifacts](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-8) | M1 — Build the usable preview image |
| I08 | [ZOS-9 — Build the shared Rust command engine](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-9) | M1 — Build the usable preview image |
| I09 | [ZOS-10 — Establish device security and manual release trust](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-10) | M1 — Build the usable preview image |
| I10 | [ZOS-11 — Implement private connectivity and remote launchers](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-11) | M1 — Build the usable preview image |
| I11 | [ZOS-12 — Package safe dev-VM service provisioning](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-12) | M1 — Build the usable preview image |
| I12 | [ZOS-13 — Deliver manual update, rollback, and recovery access](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-13) | M1 — Build the usable preview image |
| I13 | [ZOS-14 — Define the portable-profile schema and precedence](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-14) | M2 — Make a replacement laptop recoverable |
| I14 | [ZOS-15 — Implement independent enrollment and lost-device revocation](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-15) | M2 — Make a replacement laptop recoverable |
| I15 | [ZOS-16 — Implement checkpointed, idempotent restore](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-16) | M2 — Make a replacement laptop recoverable |
| I16 | [ZOS-17 — Implement actionable readiness diagnostics](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-17) | M2 — Make a replacement laptop recoverable |
| I17 | [ZOS-18 — Connect the first-run Setup Workstation journey](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-18) | M2 — Make a replacement laptop recoverable |
| I18 | [ZOS-19 — Implement deliberate preference export and local exceptions](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-19) | M2 — Make a replacement laptop recoverable |
| I19 | [ZOS-20 — Package the complete replacement-laptop runbook](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-20) | M2 — Make a replacement laptop recoverable |
| I20 | [ZOS-21 — Implement the baseline dock, shell defaults, and safe desktop](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-21) | M1 — Build the usable preview image |
| I21 | [ZOS-22 — Implement baseline native lock/login privacy and accessibility](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-22) | M1 — Build the usable preview image |
| I22 | [ZOS-23 — Implement conservative power and resume behavior](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-23) | M3 — Add advanced polish and measured power policy |
| I23 | [ZOS-24 — Automate locked builds, signing, and release records](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-24) | M4 — Maintain and qualify release channels |
| I24 | [ZOS-25 — Implement channels and client update scheduling](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-25) | M4 — Maintain and qualify release channels |
| I25 | [ZOS-26 — Implement consistent, independent dev-VM backups](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-26) | M4 — Maintain and qualify release channels |
| I26 | [ZOS-27 — Automate compatibility and data-preserving upgrade policy](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-27) | M4 — Maintain and qualify release channels |
| I27 | [ZOS-28 — Implement signer rotation and security servicing](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-28) | M4 — Maintain and qualify release channels |
| I28 | [ZOS-29 — Schedule isolated backup-restore verification](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-29) | M4 — Maintain and qualify release channels |
| I29 | [ZOS-30 — Expose dated backup health and finish operating runbooks](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-30) | M4 — Maintain and qualify release channels |

## Testing implementation and acceptance

| Plan ID | Helm card | Scope |
| --- | --- | --- |
| Q01 | [ZOS-31 — Establish the test contract and evidence ledger](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-31) | M0, harness |
| Q02 | [ZOS-32 — Build CI routing and isolation policy](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-32) | M0–M4, harness |
| Q03 | [ZOS-33 — Implement local unit, static, and content checks](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-33) | M0–M3, harness |
| Q04 | [ZOS-34 — Wire artifact provenance and payload assertions](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-34) | M0–M4, harness |
| Q05 | [ZOS-35 — Provision disposable VM and resource profiles](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-35) | M0, harness |
| Q06 | [ZOS-36 — Wire the offline-installer and suite test adapter](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-36) | M0–M1, harness |
| Q07 | [ZOS-37 — Implement the two-disk safety oracle](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-37) | M1, acceptance harness |
| Q08 | [ZOS-38 — Build the remote-workspace continuity fixture](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-38) | M0 prototype, M1 complete, harness |
| Q09 | [ZOS-39 — Add transport and session fault controls](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-39) | M1, harness |
| Q10 | [ZOS-40 — Seed populated profile/application and retained-release fixtures](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-40) | M1–M4, harness |
| Q11 | [ZOS-41 — Make independent enrollment and credential-recovery fixtures](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-41) | M2, harness |
| Q12 | [ZOS-42 — Seed backup and isolated-restore test adapters](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-42) | M2–M4, harness |
| Q13 | [ZOS-43 — Construct update, rollback, signature, and boot-trust fixtures](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-43) | M0–M4, harness |
| Q14 | [ZOS-44 — Add interrupted-operation and resource-exhaustion injection](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-44) | M1–M3, harness |
| Q15 | [ZOS-45 — Run T01 offline install and required-suite acceptance](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-45) | M1, execution |
| Q16 | [ZOS-46 — Run T03 wiped-client recovery drill and 30-minute clock](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-46) | M2, execution |
| Q17 | [ZOS-47 — Run T04 continuity and T05 transport acceptance](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-47) | M1 smoke, M2 recovery, M3 hardware, execution |
| Q18 | [ZOS-48 — Run T06 update and T07 rollback smoke/full acceptance](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-48) | M1 smoke, M2–M4 full, execution |
| Q19 | [ZOS-49 — Run T08 trust and T09 failure-safety acceptance](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-49) | M1 smoke, M2–M4 full, execution |
| Q20 | [ZOS-50 — Run T10 desktop/accessibility and T11 physical power acceptance](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-50) | M1 baseline smoke, M3 complete execution |
| Q21 | [ZOS-51 — Run T12 backup restore acceptance](https://tc.shanekanterman.dev/p/zeusos/tasks/ZOS-51) | M2–M4, execution |

Sources: [implementation plan](implementation-plan.md), [testing plan](testing-plan.md), [design baseline](design-v0.1.md).
