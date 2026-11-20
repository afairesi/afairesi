{ inputs, pkgs }:
let
  consumer = inputs.self.lib.mkFlake {
    inputs = {
      afairesi = inputs.self;
      self = consumerSelf;
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
  consumerSelf = consumer // {
    outPath = consumerSource;
    packages = builtins.mapAttrs (
      _: packages: packages // { default = packages.example; }
    ) consumer.packages;
  };
  consumerSource = ./fixtures;
  htmlPackage = inputs.self.lib.mkHtmlPackage {
    inherit pkgs;
    meta.description = "Source listing";
    postInstall = ''
      printf '%s\n' '<a href="src/main.rs">Source</a>' > "$out/index.html"
    '';
    runtimeInputs = [ (pkgs.writeShellScriptBin "http-server" ''printf '%s\0' "$@"'') ];
    src = ./fixtures/rust-native;
  };
  overridden = inputs.self.lib.mkFlake {
    inputs = {
      afairesi = inputs.self;
      self = overridden // {
        packages.${system} = {
          example = overriddenPackage;
          nix-library = pkgs.writeText "different-name" "";
        };
        outPath = consumerSource;
      };
    };
    systems = [ system ];
  };
  overriddenPackage = package.overridePythonAttrs (attributes: {
    checkInputs = [ pkgs.python3Packages.six ];
    meta = (builtins.removeAttrs attributes.meta [ "description" ]) // {
      mainProgram = "renamed";
    };
    nativeCheckInputs = [ pkgs.python3Packages.colorama ];
    passthru = attributes.passthru // {
      inherit (pkgs) lean4;
      tests.unavailable = pkgs.runCommand "excluded-check" {
        meta.badPlatforms = [ system ];
      } "exit 1";
    };
    postInstall = ''mv "$out/bin/example" "$out/bin/renamed"'';
  });
  package = consumer.packages.${system}.example;
  pythonLibrary = inputs.self.lib.mkPythonPackage {
    inherit pkgs;
    executable = false;
    postInstall = ''
      test -d "$out/lib"
      printf post >> "$out/install-hooks"
    '';
    preInstall = ''
      test ! -e "$out/lib"
      mkdir -p "$out"
      printf pre > "$out/install-hooks"
    '';
    src = ./fixtures/lean-example;
  };
  system = pkgs.stdenv.hostPlatform.system;
in
{
  inherit
    consumer
    consumerSelf
    consumerSource
    htmlPackage
    overridden
    overriddenPackage
    package
    pythonLibrary
    system
    ;
}
