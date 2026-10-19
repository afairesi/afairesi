{ hostName, ... }:
{
  boot.loader.grub.enable = false;
  fileSystems."/" = {
    device = "none";
    fsType = "tmpfs";
  };
  nixpkgs.hostPlatform = "x86_64-linux";
  networking.hostName = hostName;
  system.stateVersion = "26.05";
}
