{ inputs, pkgs }:
let
  consumerSource = ./consumer-contract;
  consumer = inputs.self.lib.mkFlake {
    inputs = {
      self = consumer // {
        outPath = consumerSource;
      };
      afairesi = inputs.self;
    };
    systems = pkgs.lib.unique [
      pkgs.stdenv.hostPlatform.system
      "x86_64-linux"
    ];
    nixpkgs.config = {
      allowUnfree = true;
      cudaSupport = true;
    };
    nixpkgs.overlays = [ (_: _: { afairesiContract = true; }) ];
  };
  system = pkgs.stdenv.hostPlatform.system;
  package = consumer.packages.${system}.example;
  vmCoverage = inputs.self.lib.mkCoverage {
    check = pkgs.vmTools.runInLinuxVM consumer.checks.${system}.example;
    packageDrv = package;
  };
  descriptionless = inputs.self.lib.mkFlake {
    inputs = {
      self = {
        outPath = ./consumer-contract;
        packages.${system}.example = package.overridePythonAttrs (attributes: {
          meta = builtins.removeAttrs attributes.meta [ "description" ];
        });
      };
      afairesi = inputs.self;
    };
    systems = [ system ];
  };
  empty = inputs.self.lib.mkFlake {
    inputs.self = empty // {
      outPath = consumerSource + "/unavailable";
    };
    systems = [ system ];
  };
  accepts =
    attributes:
    (builtins.tryEval (
      inputs.self.lib.mkPythonPackage (
        {
          inherit pkgs;
          executable = true;
          src = ./consumer-contract/example;
        }
        // attributes
      )
    )).success;
in
assert
  package.consumerConfig == {
    afairesiContract = true;
    allowUnfree = true;
    cudaSupport = true;
  };
assert package.meta.mainProgram == "example";
assert consumer.checks.${system} ? example;
assert builtins.attrNames consumer.formatter == builtins.attrNames consumer.packages;
assert consumer.checks.${system}.formatter == consumer.formatter.${system}.passthru.tests.check;
assert consumer.checks.${system}.pkgs-example == package;
assert consumer.checks.${system}.pkgs-native == consumer.packages.${system}.native;
assert
  consumer.checks.${system}.pkgs-native-contract
  == consumer.packages.${system}.native.passthru.tests.contract;
assert consumer.checks.${system}.devshell-example-test == consumer.devShells.${system}.example-test;
assert !(consumer.packages.${system} ? unavailable);
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
    nativeBuildInputs = [
      consumer.checks.${system}.example
      consumer.legacyPackages.${system}.example-coverage
      consumer.checks.${system}.pkgs-native
      consumer.checks.${system}.pkgs-native-contract
    ];
  }
  ''
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
    mkdir -p $out
  ''
