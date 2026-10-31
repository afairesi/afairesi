{
  inputs,
  pkgs,
  host,
}:
let
  inherit (pkgs) lib;
  configuration = inputs.self.nixosConfigurations.${host};
  hasDisko = builtins.attrNames (configuration.config.disko.devices or { }) != [ ];
  instrumented = configuration.extendModules {
    modules = [
      (
        if hasDisko then
          {
            virtualisation.vmVariantWithDisko.virtualisation.graphics = lib.mkForce false;
          }
        else
          {
            virtualisation.vmVariant.virtualisation.graphics = lib.mkForce false;
          }
      )
      ({ modulesPath, ... }: {
        imports = [ (modulesPath + "/testing/test-instrumentation.nix") ];
        users.users.root.initialHashedPassword = lib.mkForce null;
      })
    ];
  };
  name = "${host}Vm";
  preservedPaths = lib.concatLists (
    lib.mapAttrsToList (
      path: state:
      let
        directories = state.directories ++ lib.concatMap (user: user.directories) users;
        files = state.files ++ lib.concatMap (user: user.files) users;
        users = builtins.attrValues state.users;
      in
      map (file: {
        inherit (file) how;
        directory = false;
        path = file.file;
        persistent = path + file.file;
      }) files
      ++ map (directory: {
        inherit (directory) how;
        directory = true;
        path = directory.directory;
        persistent = path + directory.directory;
      }) (builtins.filter (directory: directory.how != "_intermediate") directories)
    ) (vmConfig.preservation.preserveAt or { })
  );
  startScript = pkgs.writeShellScript "start-${name}" (
    if hasDisko then
      ''
        set -e
        ${pkgs.util-linux}/bin/setsid ${vm}/bin/disko-vm "$@" &
        vm_pid=$!
        trap 'kill -- -"$vm_pid" 2>/dev/null || true; wait "$vm_pid" 2>/dev/null || true' EXIT
        trap 'exit 130' INT
        trap 'exit 143' TERM
        wait "$vm_pid"
      ''
    else
      ''
        exec ${vm}/bin/run-*-vm "$@"
      ''
  );
  vm = if hasDisko then vmConfig.system.build.vmWithDisko else vmConfig.system.build.vm;
  vmConfig =
    if hasDisko then
      instrumented.config.virtualisation.vmVariantWithDisko
    else
      instrumented.config.virtualisation.vmVariant;
in
pkgs.testers.runNixOSTest {
  inherit name;
  globalTimeout = 600;
  nodes = { };
  requiredFeatures.kvm = pkgs.stdenv.hostPlatform.isLinux;
  testScript = ''
    import json
    import shlex
    machine = create_machine(start_command="${startScript}", name="machine")
    driver.machines_qemu.append(machine)
    machine.start(allow_reboot=True)
    for phase in ["boot", "reboot"]:
        with subtest(phase):
            machine.wait_for_unit("local-fs.target")
            machine.wait_for_unit("multi-user.target")
            for entry in json.loads(${builtins.toJSON (builtins.toJSON preservedPaths)}):
                path = shlex.quote(entry["path"])
                persistent = shlex.quote(entry["persistent"])
                kind = "d" if entry["directory"] else "f"
                machine.succeed(f"test -{kind} {path}; test -{kind} {persistent}")
                if entry["how"] == "symlink":
                    assert machine.succeed(f"readlink -f {path}").strip() == machine.succeed(f"readlink -f {persistent}").strip()
                else:
                    assert machine.succeed(f"stat -Lc '%d:%i' {path}").strip() == machine.succeed(f"stat -Lc '%d:%i' {persistent}").strip()
                if entry["directory"]:
                    marker = shlex.quote(entry["path"] + "/.afairesi-preservation-probe")
                    if phase == "boot":
                        machine.succeed(f"printf afairesi-preserved > {marker}")
                    else:
                        assert machine.succeed(f"cat {marker}") == "afairesi-preserved"
        if phase == "boot":
            machine.reboot()
    machine.shutdown()
  '';
}
