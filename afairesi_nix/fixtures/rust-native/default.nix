{ pkgs, pname, ... }:
pkgs.rustPlatform.buildRustPackage {
  inherit pname;
  cargoLock.lockFile = ./Cargo.lock;
  meta = {
    description = "A standard Rust package with its own Cargo layout";
    mainProgram = "rust-native";
  };
  src = ./.;
  version = "0.0.0";
}
