{ inputs, pkgs }:
let
  consumer = inputs.self.blueprint {
    inputs = {
      self = consumer // {
        outPath = ./consumer-contract;
      };
      perigrafo = inputs.self;
    };
    systems = [ pkgs.stdenv.hostPlatform.system ];
    nixpkgs.config = {
      allowUnfree = true;
      cudaSupport = true;
    };
  };
  system = pkgs.stdenv.hostPlatform.system;
  package = consumer.packages.${system}.example;
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
assert !(inputs.self.lib ? mkFlake);
pkgs.runCommand "perigrafo-consumer-contract" { } "mkdir -p $out"
