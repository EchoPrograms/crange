# Capture and inspect releases

A release is an immutable, versioned manifest plus the captured machine images it references. Capture once the staging machines contain the exercise content and their declared resources, networking and checks are correct.

## Capture

Choose **Capture release** in Workspace or Releases. Enter a new version, choose the intended machines and declare boot-order permutations when needed. Watch the task output. Source machines may be stopped for consistent complete copies and restarted afterward, so allow a staging interruption.

Capture exports and verifies immutable artifacts and records their identities. It can involve large disk reads/writes, backup compression, hashing and temporary copies. Completion of one native copy is not completion of the whole release. Disk I/O congestion can make these phases long even when CPU use is modest.

The manifest records release identity/version, source revision, machine definitions/resources, image references, addresses/networks, health checks, boot orders, creation metadata and allowed overrides. Test and deployment records track later use without modifying the immutable source version.

## Inspect a version

Open the release version or its **Inspect capture** action. Compare captured disk/image resources with the source definitions and expected machine count. Use the retained task/journal if capture failed; restarting capture blindly can duplicate temporary allocations.

A source machine's visible disk can remain **64 GiB** while its captured copy is **30 or 31 GiB**. These are different objects. Staging tables describe staging; release metadata and clone configuration describe the captured result. A small compressed archive or low used space does not establish a smaller virtual disk.

## Smaller captured disks

The current reduced Windows capture implementation is limited to approved Project 100 domain-controller/file-server profiles and its fixed isolated target. It is not a general GUI shrink tool for arbitrary guests. The form's **Captured image disk (GiB)** defaults to retaining source size; the supported reduction works on a full disposable copy and checks guest filesystem layout, headroom and native disk evidence.

Original source disks and snapshots are preserved. An unknown write/resize outcome retains the copy and journal for reconciliation. Do not delete or repeat against the same guest until the native task and guest state are known.

## Keep older versions

Use a new version to change captured machines or checks. Do not edit a release in place or overwrite its archive. Deployed linked clones and snapshots can depend on older local bases. Retirement and cache reclamation need a fresh ownership/dependency review, not merely an unused-looking row.

Continue with [fresh-copy testing](release-tests.md), then [team deployment](deployments.md).
