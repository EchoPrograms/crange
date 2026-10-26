# Your first lab

This walkthrough creates a small web-server exercise. Use an administrator-approved Linux image that supports the chosen remote service. Names and addresses below are examples; use your assigned project and available addresses.

## 1. Open your project

Sign in to CRANGE Control and select your project in the sidebar. Open **Project Overview** to check its team list, resource limits and recent task results. If the project has no deployed router foundation, ask the administrator to complete its reviewed bootstrap first.

![Project overview with allocation and management links](/img/screenshots/overview.png)

## 2. Add a staging machine

Open **Scenario → Workspace → Add VM**. Give the machine a clear name such as `web-01`, choose an approved Linux image, select Team 0's default network, and choose a free static host octet such as `20`. Start with resources appropriate to the image and project quota. Select SSH only if the image already provides it.

The form previews the resulting address. Do not use `.1` or the DHCP range for a static scenario server. Add an HTTP check for `/health` on port 80, expected status 200, if your application will provide that endpoint. You can edit the checks later.

![The Add VM form in the actual interface](/img/screenshots/add-machine.png)

Save the declaration. Select **Review changes**, open **Deployment plan**, and read the resource actions. The first review can take time while an approved image cache is prepared. Use **Deploy to staging** for the reviewed plan. Watch its task output until it completes.

## 3. Configure the exercise inside the guest

Connect to the deployed VM. Install your web application and configure its database connections, application accounts and other exercise content inside the guest, or with a script you maintain in the lab. CRANGE's role is the platform lifecycle and common network/access configuration.

Create `/health` so that it answers only when the application is ready, then use **Run health checks**. Also perform any manual checks relevant to your exercise. A TCP connection or ping alone does not establish application correctness.

## 4. Capture a version

Choose **Capture release** and enter a new version such as `1.0.0`. Include the intended staging machines and, for multi-machine scenarios, the startup orders you want tested. Capture may stop source guests temporarily and restart them afterward. Check the task result before continuing.

Open **Releases** and inspect the captured version. Check machine count, captured image identity, network addresses, resources and checks. The release is immutable.

## 5. Test fresh copies

On that release, choose **Test release**. This prepares disposable test definitions; it does not mean they have been deployed. Follow **Workspace** to review and deploy the test resources. A new custom test network may first require its router foundation and **Verify network**, followed by a new review to deploy the machines.

When the release shows all expected test copies deployed, choose **Check test**. Open the progress and wait for the actual result. Fix failures before treating the version as accepted. When it passes, choose **Clean up test**; cleanup now reviews and applies removal in the same job. Wait until **Cleanup pending** clears.

![Saved releases and their fresh-copy results](/img/screenshots/releases.png)

## 6. Deploy the selected version

Open **Team deployments → Choose a release** and select the passing version. Leave **Team numbers** empty for all declared regular teams, or enter a subset such as `1, 2`. Team 0 is not included as a regular deployment target.

Review the rollout, inspect the saved plan, then apply the reviewed rollout. Check the task and team rows. Infrastructure creation and guest configuration are separate phases; if creation succeeded and configuration failed, use **Continue configuration** after fixing the cause.

![The version chooser with readiness reasons](/img/screenshots/release-picker.png)

Assign project users to their team/default jump box or explicit machines in **Project users**. Open one assigned connection to confirm the participant's actual access path. Your first lab now has a versioned source, a recorded test and an explicit team rollout.

For a replacement or upgrade, review its deletion/replacement actions and data consequences first. Guest-local changes are not automatically preserved across rebuilding a team from a release.
