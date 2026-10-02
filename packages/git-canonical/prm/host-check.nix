{
  inputs,
  pkgs,
  ...
}:
let
  inherit (pkgs) lib;
  configuration = inputs.self.nixosConfigurations.${host};
  hasDisko = builtins.attrNames (configuration.config.disko.devices or { }) != [ ];
  host = lib.removeSuffix "VmWithDisko" (baseNameOf ./.);
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
      })
    ];
  };
  name = "${host}VmWithDisko";
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
  requiredFeatures.kvm = pkgs.stdenv.hostPlatform.isLinux;
  testScript = ''
    machine = create_machine(start_command="${startScript}", name="machine")
    driver.machines_qemu.append(machine)
    machine.start(allow_reboot=True)
    for phase in ["boot", "reboot"]:
        with subtest(phase):
            machine.wait_for_unit("local-fs.target")
            machine.wait_for_unit("multi-user.target")
            ${lib.optionalString vmConfig.services.openssh.enable ''
              machine.wait_for_unit("sshd.service")
            ''}
        if phase == "boot":
            machine.reboot()
    machine.shutdown()
  '';
}
