{
  inputs,
  pkgs,
  host,
}:
let
  inherit (pkgs) lib;
  bootstrapNode =
    seeded: _:
    {
      boot.initrd.systemd = {
        inherit (hostConfig.boot.initrd.systemd) enable;
        storePaths = [ fixture ];
      };
      imports = [
        inputs.preservation.nixosModules.default
      ]
      ++ lib.optional hasAge inputs.agenix.nixosModules.default;
      preservation = {
        enable = true;
        preserveAt = lib.mapAttrs (path: state: {
          directories =
            map
              (directory: {
                inherit (directory)
                  configureParent
                  createLinkTarget
                  directory
                  group
                  how
                  inInitrd
                  mode
                  mountOptions
                  parent
                  user
                  ;
              })
              (
                builtins.filter (
                  directory: lib.any (key: lib.hasPrefix (path + directory.directory + "/") key.preserved) keys
                ) state.directories
              );
          files =
            map
              (file: {
                inherit (file)
                  configureParent
                  createLinkTarget
                  file
                  group
                  how
                  inInitrd
                  mode
                  mountOptions
                  parent
                  user
                  ;
              })
              (
                builtins.filter (
                  file:
                  lib.any (key: path + file.file == key.preserved || path + file.file == key.preserved + ".pub") keys
                ) state.files
              );
        }) hostConfig.preservation.preserveAt;
      };
      services.openssh = {
        inherit (hostConfig.services.openssh) enable hostKeys;
      };
      system.stateVersion = hostConfig.system.stateVersion;
      systemd.services.sshd.preStart = lib.optionalString (hasAge && seeded) ''
        ${pkgs.gnugrep}/bin/grep -qx afairesi-bootstrap-ok /run/agenix/afairesi-probe
      '';
      testing.initrdBackdoor = true;
      virtualisation = {
        diskImage = null;
        emptyDiskImages = lib.imap0 (index: _: {
          driveConfig.deviceExtraOpts.serial = "preserved-${toString index}";
          size = 64;
        }) storage;
        fileSystems = builtins.listToAttrs (
          lib.imap0 (index: state: {
            name = state.path;
            value = {
              autoFormat = true;
              device = "/dev/disk/by-id/virtio-preserved-${toString index}";
              fsType = "ext4";
              neededForBoot = hostConfig.fileSystems.${state.path}.neededForBoot or false;
            };
          }) storage
        );
        memorySize = 1024;
      };
    }
    // lib.optionalAttrs hasAge {
      age = {
        inherit (hostConfig.age) identityPaths;
        secrets = lib.optionalAttrs seeded {
          afairesi-probe.file = "${fixture}/probe.age";
        };
      };
    };
  configuration = inputs.self.nixosConfigurations.${host};
  fixture =
    pkgs.runCommand "${host}-disposable-bootstrap-identities"
      {
        nativeBuildInputs = [ pkgs.openssh ] ++ lib.optional hasAge pkgs.age;
      }
      ''
        mkdir -p "$out"
        ${lib.concatMapStrings (key: ''
          ssh-keygen -q -t ${lib.escapeShellArg key.type} \
            ${lib.optionalString (key ? bits) "-b ${toString key.bits}"} \
            -N "" -C disposable-afairesi-test -f "$out/key-${toString key.index}"
        '') keys}
        ${lib.optionalString hasAge ''
          ${
            assert lib.assertMsg (
              identityKeys != [ ]
            ) "Afairesi bootstrap check: agenix needs a preserved SSH host identity";
            ""
          }
          printf 'afairesi-bootstrap-ok\n' | age \
            ${lib.concatMapStringsSep " " (key: "-R \"$out/key-${toString key.index}.pub\"") identityKeys} \
            -o "$out/probe.age"
        ''}
      '';
  hasAge = hasBootstrap && builtins.attrNames (hostConfig.age.secrets or { }) != [ ];
  hasBootstrap = hasPreservation && hostConfig.services.openssh.enable;
  hasDisko = builtins.attrNames (configuration.config.disko.devices or { }) != [ ];
  hasPreservation = hostConfig.preservation.enable or false;
  hostConfig = configuration.config;
  identityKeys = builtins.filter (
    key:
    builtins.elem key.type [
      "rsa"
      "ed25519"
    ]
    && (
      builtins.elem key.path hostConfig.age.identityPaths
      || builtins.elem key.preserved hostConfig.age.identityPaths
    )
  ) keys;
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
  keys = lib.imap0 (
    index: key:
    let
      directories =
        lib.sort (a: b: builtins.stringLength a.directory > builtins.stringLength b.directory)
          (
            builtins.filter (
              directory:
              lib.hasPrefix (directory.directory + "/") key.path
              || lib.hasPrefix (directory.persistent + "/") key.path
            ) preservedDirectories
          );
      files = builtins.filter (file: key.path == file.file || key.path == file.persistent) preservedFiles;
      preserved =
        if files != [ ] then
          (builtins.head files).persistent
        else if directories != [ ] then
          let
            directory = builtins.head directories;
          in
          if lib.hasPrefix (directory.persistent + "/") key.path then
            key.path
          else
            directory.persistent + lib.removePrefix directory.directory key.path
        else
          throw "Afairesi bootstrap check: SSH host key ${key.path} is not preserved";
    in
    key // { inherit index preserved; }
  ) hostConfig.services.openssh.hostKeys;
  name = "${host}VmWithDisko";
  preservedDirectories = lib.concatMap (
    state:
    map (directory: {
      inherit (directory) directory how;
      persistent = state.path + directory.directory;
    }) state.directories
  ) storage;
  preservedFiles = lib.concatMap (
    state:
    map (file: {
      inherit (file) file how;
      persistent = state.path + file.file;
    }) state.files
  ) storage;
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
  storage = lib.mapAttrsToList (path: state: {
    inherit path;
    inherit (state) files;
    inherit (state) directories;
  }) (hostConfig.preservation.preserveAt or { });
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
  nodes = lib.optionalAttrs hasBootstrap {
    fresh = bootstrapNode false;
    seeded = bootstrapNode true;
  };
  requiredFeatures.kvm = pkgs.stdenv.hostPlatform.isLinux;
  testScript = ''
    import json
    import shlex
    ${lib.optionalString hasBootstrap ''
      keys = json.loads(${builtins.toJSON (builtins.toJSON keys)})
      fixture = ${builtins.toJSON (toString fixture)}
      def check_host_keys(node):
          node.wait_for_unit("sshd.service")
          checksums = []
          for key in keys:
              path = shlex.quote(key["path"])
              preserved = shlex.quote(key["preserved"])
              node.succeed(f"test -s {preserved}")
              assert node.succeed(f"stat -Lc '%a %U %G' {path}").strip() == "600 root root"
              node.succeed(f"cmp {path} {preserved}")
              public = node.succeed(f"ssh-keygen -y -f {path}").split()
              saved = node.succeed("cat " + shlex.quote(key["path"] + ".pub")).split()
              assert public[:2] == saved[:2]
              checksums.append(node.succeed(f"sha256sum {preserved}"))
          return checksums
      for node in [seeded, fresh]:
          node.start(allow_reboot=True)
          node.wait_for_unit("default.target")
      with subtest("Bootstrap with provisioned SSH identities"):
          for key in keys:
              source = shlex.quote(fixture + "/key-" + str(key["index"]))
              destination = shlex.quote("/sysroot" + key["preserved"])
              seeded.succeed(f"install -D -m 0600 {source} {destination}")
              source_public = shlex.quote(fixture + "/key-" + str(key["index"]) + ".pub")
              destination_public = shlex.quote("/sysroot" + key["preserved"] + ".pub")
              seeded.succeed(f"install -D -m 0644 {source_public} {destination_public}")
          seeded.switch_root()
          check_host_keys(seeded)
          for key in keys:
              source = shlex.quote(fixture + "/key-" + str(key["index"]))
              destination = shlex.quote(key["preserved"])
              seeded.succeed(f"cmp {source} {destination}")
          ${lib.optionalString hasAge ''seeded.succeed("grep -qx afairesi-bootstrap-ok /run/agenix/afairesi-probe")''}
      with subtest("Generate SSH identities on empty persistent storage"):
          for key in keys:
              fresh.fail("test -e " + shlex.quote("/sysroot" + key["preserved"]))
          fresh.switch_root()
          check_host_keys(fresh)
      for node in [seeded, fresh]:
          with subtest(f"{node.name}: SSH identities survive a clean-root reboot"):
              original = check_host_keys(node)
              node.succeed("touch /afairesi-unpreserved-marker")
              node.reboot()
              node.wait_for_unit("default.target")
              node.fail("test -e /sysroot/afairesi-unpreserved-marker")
              node.switch_root()
              assert check_host_keys(node) == original
      ${lib.optionalString hasAge ''
        with subtest("Decrypt again after reboot without reprovisioning"):
            seeded.succeed("grep -qx afairesi-bootstrap-ok /run/agenix/afairesi-probe")
      ''}
      for node in [seeded, fresh]:
          node.shutdown()
    ''}
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
