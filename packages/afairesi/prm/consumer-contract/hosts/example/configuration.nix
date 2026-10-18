{ ... }:
{
  boot.loader.grub.enable = false;
  fileSystems."/" = {
    device = "none";
    fsType = "tmpfs";
  };
  nixpkgs.hostPlatform = "x86_64-linux";
  system.stateVersion = "26.05";
}
