# Build and edit staging

Open **Scenario → Workspace**. Team 0 is the working copy of your scenario; its guest changes do not update existing team deployments.

## Add a VM or container

Choose **Add VM** or **Add container**. Select an approved OS/image, set CPU and RAM, select the default or an owned custom network, and choose a free static host octet. For a VM whose approved image has verified system-disk metadata, you can request 8–1024 GiB, at least the image's operating-system disk size. A small base can be cloned with a larger requested disk without changing the shared image. The form shows the full IP and a GiB equivalent for RAM. Remote protocol choices publish connection settings; the guest must already run the selected service.

VMs have their own virtual hardware/kernel; containers use an approved LXC root filesystem and share the host kernel. Available images and nodes depend on administrator approval, provider compatibility and network acceptance.

![Staging machine declarations and their runtime status](/img/screenshots/workspace.png)

## Review and deploy

1. Save your declarations. Use **Review changes** to prepare eligible missing local image caches and retain a Terraform plan.
2. Expand **Deployment plan**. Check creates, changes, deletions/replacements, image, node and storage. Review may create approved cache templates, but it does not deploy scenario guests.
3. Use **Deploy to staging** for the retained review. Changes affecting shared/project infrastructure require the corresponding infrastructure review rather than being silently included in a staging apply.
4. Follow the task through apply, platform configuration and remote publication. Run your health/manual checks afterward.

Saved reviews expire after an hour and bind the inputs/state they reviewed. If they are stale, review again. An empty infrastructure plan can still accept staged settings; it does not prove an application works.

A new custom network may produce a foundation-only plan. Deploy its router/network, verify the network, then review again for the guest deployment. See [networks](networks.md).

## Configure your scenario

Connect to each guest to install software and exercise data. Keep application configuration under your control, whether performed manually or by a script you place inside the lab. Declare common networking and access settings in CRANGE where supported. An image without supported guest customization may require manual IP configuration.

Use machine power actions to try realistic startup orders before capture. Starting a guest is a live operation; changing a definition's saved power flag is desired state. Read both the runtime status and task output.

## Edit or remove a machine

Use **Edit**, save, review and deploy. CPU/RAM increases must fit quota and placement limits; some changes need a guest stop/start before taking effect. Removing a definition requires reviewed deletion before its native guest is gone. Examine every deletion/replacement and preserve required data first.

Staging originals are separate from immutable captured images. The captured-image reduction workflow, where supported, works on an isolated full copy. Changing a capture size is not an instruction to shrink a running source disk; see [capture details](releases.md).
