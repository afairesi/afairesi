{ inputs, pkgs }:
let
  accepts =
    attributes:
    (builtins.tryEval (
      inputs.self.lib.mkPythonPackage (
        {
          inherit pkgs;
          executable = true;
          src = ./prm/consumer-contract/example;
        }
        // attributes
      )
    )).success;
  acceptsLean =
    attributes:
    (builtins.tryEval (
      inputs.self.lib.mkLean4Package (
        {
          inherit pkgs;
          executable = true;
          src = ./prm/consumer-contract/lean-example;
        }
        // attributes
      )
    )).success;
  consumer = inputs.self.lib.mkFlake {
    inputs = {
      afairesi = inputs.self;
      self = consumer // {
        outPath = consumerSource;
      };
    };
    nixpkgs = {
      config = {
        allowUnfree = true;
        cudaSupport = true;
      };
      overlays = [ (_: _: { afairesiContract = true; }) ];
    };
    systems = pkgs.lib.unique [
      pkgs.stdenv.hostPlatform.system
      "x86_64-linux"
    ];
  };
  consumerSource = ./prm/consumer-contract;
  descriptionless = inputs.self.lib.mkFlake {
    inputs = {
      afairesi = inputs.self;
      self = {
        packages.${system}.example = package.overridePythonAttrs (attributes: {
          meta = builtins.removeAttrs attributes.meta [ "description" ];
        });
        outPath = ./prm/consumer-contract;
      };
    };
    systems = [ system ];
  };
  empty = inputs.self.lib.mkFlake {
    inputs.self = empty // {
      outPath = consumerSource + "/unavailable";
    };
    systems = [ system ];
  };
  leanExample = consumer.packages.${system}.lean-example;
  leanLibrary = consumer.packages.${system}.test_main;
  package = consumer.packages.${system}.example;
  system = pkgs.stdenv.hostPlatform.system;
  vmCoverage = inputs.self.lib.mkCoverage {
    check = pkgs.vmTools.runInLinuxVM consumer.checks.${system}.example;
    packageDrv = package;
  };
in
assert
  package.consumerConfig == {
    afairesiContract = true;
    allowUnfree = true;
    cudaSupport = true;
  };
assert package.meta.mainProgram == "example";
assert consumer.checks.${system} ? example;
assert consumer.checks.${system} ? lean-example;
assert consumer.checks.${system} ? test_main;
assert consumer.checks.${system}.pkgs-lean-example == leanExample;
assert consumer.checks.${system}.pkgs-test_main == leanLibrary;
assert
  consumer.checks.${system}.devshell-lean-example-test
  == consumer.devShells.${system}.lean-example-test;
assert leanExample.meta.mainProgram == "lean-example";
assert !(leanLibrary.meta ? mainProgram);
assert !(leanExample.drvAttrs ? runtimeHook);
assert !(consumer.legacyPackages.${system} ? lean-example-coverage);
assert !(consumer.apps.${system} ? test_main);
assert inputs.self.lib ? mkLean4Package;
assert !(acceptsLean { shellHook = "echo obsolete"; });
assert
  !(acceptsLean {
    executable = false;
    runtimeHook = "echo invalid";
  });
assert builtins.attrNames consumer.formatter == builtins.attrNames consumer.packages;
assert consumer.checks.${system}.formatter == consumer.formatter.${system}.passthru.tests.check;
assert consumer.checks.${system}.pkgs-example == package;
assert consumer.checks.${system}.pkgs-native == consumer.packages.${system}.native;
assert
  consumer.checks.${system}.pkgs-native-contract
  == consumer.packages.${system}.native.passthru.tests.contract;
assert consumer.checks.${system}.devshell-example-test == consumer.devShells.${system}.example-test;
assert !(consumer.packages.${system} ? unavailable);
assert !(consumer.checks.${system} ? unavailable);
assert !(consumer.checks.${system} ? orphan);
assert consumer.checks.${system} ? nix-library;
assert consumer.checks.${system} ? nix-tests;
assert !(consumer.checks.${system} ? nix-untested);
assert !(consumer.checks.${system} ? native);
assert !(consumer.devShells.${system} ? nix-library-test);
assert !(consumer.legacyPackages.${system} ? nix-library-coverage);
assert consumer.packages.${system}.native.example == package;
assert consumer.nixosConfigurations.example.pkgs.config.allowUnfree;
assert consumer.nixosConfigurations.example.pkgs.config.cudaSupport;
assert consumer.nixosConfigurations.example.pkgs.afairesiContract;
assert consumer.nixosConfigurations.example.config.networking.hostName == "example";
assert (consumer.checks.${system} ? nixos-example) == (system == "x86_64-linux");
assert empty.packages.${system} == { };
assert builtins.attrNames empty.formatter == [ system ];
assert builtins.attrNames empty.checks.${system} == [ "formatter" ];
assert empty.apps.${system} == { };
assert empty.devShells.${system} == { };
assert empty.legacyPackages.${system} == { };
assert empty.nixosConfigurations == { };
assert !(builtins.pathExists (consumerSource + "/checks"));
assert (consumer.checks.${system} ? exampleVm) == (system == "x86_64-linux");
assert system != "x86_64-linux" || builtins.isString consumer.checks.${system}.exampleVm.drvPath;
assert consumer.legacyPackages.${system} ? example-test-environment;
assert consumer.legacyPackages.${system} ? example-coverage;
assert !pkgs.stdenv.hostPlatform.isLinux || vmCoverage.requiredSystemFeatures == [ "kvm" ];
assert
  !pkgs.stdenv.hostPlatform.isLinux
  || vmCoverage.origBuilder == consumer.checks.${system}.example.builder;
assert
  !pkgs.stdenv.hostPlatform.isLinux
  || vmCoverage.drvPath != consumer.legacyPackages.${system}.example-coverage.drvPath;
assert consumer.devShells.${system} ? example-test;
assert consumer.apps.${system}.example.type == "app";
assert consumer.apps.${system}.example.meta == package.meta;
assert consumer.apps.${system}.example.meta.description == "Afairesi consumer contract example";
assert descriptionless.apps.${system}.example.meta.description == "Run example";
assert descriptionless.apps.${system}.example.meta.mainProgram == "example";
assert !(package.drvAttrs ? runtimeHook);
assert !(accepts { shellHook = "echo obsolete"; });
assert !(accepts { src = "/invalid/123"; });
assert !(accepts { src = "/invalid/class"; });
assert
  !(accepts {
    executable = false;
    runtimeHook = "echo invalid";
  });
assert !(inputs.self.apps.${system} ? afairesi);
assert !(inputs.self.lib ? mkPythonCheck);
assert !(inputs.self.lib ? mkHostCheck);
assert inputs.self.lib ? mkFlake;
assert !(inputs.self ? blueprint);
assert !(inputs.self.lib ? blueprint);
assert inputs.self.packages.${system}.default == inputs.self.packages.${system}.afairesi;
pkgs.runCommand "afairesi-consumer-contract"
  {
    leanShell = consumer.devShells.${system}.lean-example-test;
    nativeBuildInputs = [
      consumer.checks.${system}.example
      consumer.legacyPackages.${system}.example-coverage
      consumer.checks.${system}.pkgs-native
      consumer.checks.${system}.pkgs-native-contract
      consumer.checks.${system}.nix-library
      consumer.checks.${system}.nix-tests
      consumer.checks.${system}.lean-example
      consumer.checks.${system}.test_main
      consumer.formatter.${system}.passthru.raw
    ];
  }
  ''
    test "$(cat ${consumer.packages.${system}.nix-untested})" = untested
    unset AFAIRESI_CONSUMER_HOOK
    expected=$(printf '%s\n' loaded "['two words', '--flag']")
    actual=$(${consumer.apps.${system}.example.program} 'two words' --flag)
    test "$actual" = "$expected"
    expected=$(printf '%s\n' unloaded "['two words', '--flag']")
    actual=$(${pkgs.lib.getExe package} 'two words' --flag)
    test "$actual" = "$expected"
    export AFAIRESI_CONSUMER_HOOK=server
    test "$(${pkgs.lib.getExe package} | head -n1)" = server
    status=0
    AFAIRESI_CONSUMER_FAIL=1 ${consumer.apps.${system}.example.program} > failed-output || status=$?
    test "$status" = 23
    test ! -s failed-output
    expected=$(printf '%s\n' '42:resource:loaded' 'two words' '--flag')
    actual=$(${consumer.apps.${system}.lean-example.program} 'two words' --flag)
    test "$actual" = "$expected"
    expected=$(printf '%s\n' '42:resource:unloaded' 'two words' '--flag')
    actual=$(${pkgs.lib.getExe leanExample} 'two words' --flag)
    test "$actual" = "$expected"
    test ! -e ${leanLibrary}/bin
    cmp ${leanLibrary.src}/test_main.lean ${leanLibrary}/test_main.lean
    cp -R ${consumerSource} formatted
    chmod -R u+w formatted
    cd formatted
    touch flake.nix
    printf '%s\n' '-- ordinary explanation' '/- outer /- nested -/ explanation -/' >> lean-example/main.lean
    cat >> lean-example/main.lean <<'LEAN'
    example : String := "-- literal /- literal"
    example : String := r#""-- raw literal /- literal"#
    example : String := s!"{("-- interpolation")} /- text"
    example : Nat := 1 /- inline explanation -/ + 2
    LEAN
    sed -i 's/$/\r/' lean-example/main.lean
    cp lean-example/prm/Support.lean expected-support.lean
    treefmt --no-cache {lean-example,test_main}/{main,test_main}.lean
    treefmt --no-cache --fail-on-change {lean-example,test_main}/{main,test_main}.lean
    grep -F -- '-- lean-fmt: ignore[FMT005]' lean-example/main.lean
    ! grep -F 'ordinary explanation' lean-example/main.lean
    ! grep -F 'nested -/ explanation' lean-example/main.lean
    ! grep -F 'inline explanation' lean-example/main.lean
    grep -F '"-- literal /- literal"' lean-example/main.lean
    grep -F 'r#""-- raw literal /- literal"#' lean-example/main.lean
    grep -F '"-- interpolation"' lean-example/main.lean
    grep -F '/-- Print the dependency value' lean-example/main.lean
    cmp expected-support.lean lean-example/prm/Support.lean
    grep -F 'set_option linter.missingDocs false in' test_main/main.lean
    cp test_main/main.lean expected-main.lean
    cp ${consumerSource}/test_main/main.lean test_main/main.lean
    printf '%s\n' 'import MissingFormatterDependency' > test_main/test_main.lean
    if treefmt --no-cache test_main/{main,test_main}.lean > format-failure.log 2>&1; then
      exit 1
    fi
    cmp expected-main.lean test_main/main.lean
    cp ${consumerSource}/test_main/main.lean test_main/main.lean
    cat > test_main/test_main.lean <<'LEAN'
    #eval show IO Unit from do
      let some target ← IO.getEnv "AFAIRESI_EDIT_TARGET"
        | throw (IO.userError "missing edit target")
      let marker := System.FilePath.mk (target ++ ".edited")
      unless ← marker.pathExists do
        IO.FS.writeFile marker ""
        IO.FS.writeFile target "-- concurrent edit\n"
    LEAN
    printf '%s\n' '-- concurrent edit' > expected-edit.lean
    status=0
    AFAIRESI_EDIT_TARGET="$PWD/test_main/main.lean" \
      treefmt --no-cache test_main/{main,test_main}.lean > concurrent-format.log 2>&1 || status=$?
    cmp expected-edit.lean test_main/main.lean
    test "$status" -ne 0
    mkdir -p $out
  ''
