# Platform settings and application ownership

CRANGE should make common lab operations intuitive without becoming the owner of every exercise's application configuration.

## What belongs to the platform

Common, repeatable settings belong in the UI and controlled platform workflows: team-derived IP/subnet/gateway/DNS settings where the image supports them; CPU/RAM/disk declarations; approved network/image/storage selection; remote-access publication; supported guest access baseline; power, snapshots, captures, versions and explicit rollout.

These settings describe how the lab fits into the range. They should not require each manager to build a private Terraform/Ansible stack or discover incident-specific repair buttons.

## What belongs to the lab manager

The manager owns service configuration and validation: database connection strings, application configuration files, domain/exercise content, seed data, application accounts and the definition of a healthy scenario. Configure these inside the machines manually or with a script the lab manager maintains for that lab.

A manager-owned script does not mean arbitrary root shell on the controller. The existing GUI does not provide a general-purpose arbitrary script execution facility. A future supported hook would need a clear execution location, input/output contract and permissions; this manual does not claim such an editor/runner exists today.

Use hostnames or a deliberate manager script when an application's references vary with team number. Declaring an IP should not imply CRANGE will rewrite arbitrary application files.

## Verification

Managers choose health checks and perform manual functional checks. Infrastructure can verify ownership, network/address allocation, task completion and platform prerequisites without asserting that every exercise service is correct. Passing ping, a TCP connection and a complete business transaction establish different things.

Fresh-copy testing is useful because it proves the captured version can be instantiated and checked from a clean copy. Keep its meaning tied to the checks actually defined and run.

## Current exception

The checked-in Project 100/MHL workflows still include release-bound Windows/domain/file-service and Linux/business service recipes, with application configuration and service verification invoked during release configuration. Their source identities are pinned to release records. They remain implemented legacy behavior, and decoupling them is unfinished work.

For this existing workflow, Windows/DNS configuration runs before Linux/business configuration because the latter uses domain DNS. Do not treat these fixed project-specific recipes as a required model for new labs. The desired general contract is platform-owned common settings and manager-owned applications/checks.
