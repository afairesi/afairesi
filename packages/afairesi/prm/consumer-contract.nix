{ inputs, pkgs }:
let
  consumer = inputs.self.blueprint {
    inputs = {
      self = consumer // {
        outPath = ./consumer-contract;
      };
      afairesi = inputs.self;
    };
    systems = [ pkgs.stdenv.hostPlatform.system ];
    nixpkgs.config = {
      allowUnfree = true;
      cudaSupport = true;
    };
  };
  system = pkgs.stdenv.hostPlatform.system;
  package = consumer.packages.${system}.example;
  accepts = attributes:
    (builtins.tryEval (inputs.self.lib.mkPythonPackage ({
      inherit pkgs;
      executable = true;
      src = ./consumer-contract/packages/example;
    } // attributes))).success;
in
assert
  package.consumerConfig == {
    allowUnfree = true;
    cudaSupport = true;
  };
assert package.meta.mainProgram == "example";
assert consumer.checks.${system} ? example;
assert consumer.legacyPackages.${system} ? example-test-environment;
assert consumer.legacyPackages.${system} ? example-coverage;
assert consumer.devShells.${system} ? example-test;
assert consumer.apps.${system}.example.type == "app";
assert consumer.apps.${system}.example.meta == package.meta;
assert consumer.apps.${system}.example.meta.description == "Afairesi consumer contract example";
assert !(package.drvAttrs ? runtimeHook);
assert !(accepts { shellHook = "echo obsolete"; });
assert !(accepts { executable = false; runtimeHook = "echo invalid"; });
assert !(inputs.self.apps.${system} ? afairesi);
assert !(inputs.self.lib ? mkFlake);
pkgs.runCommand "afairesi-consumer-contract" { } ''
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
