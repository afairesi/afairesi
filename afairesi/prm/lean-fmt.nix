{ pkgs }:
pkgs.leanPackages.buildLakePackage {
  pname = "lean-fmt";
  version = "4.35.0";
  src = pkgs.fetchFromGitHub {
    owner = "jcreinhold";
    repo = "lean-fmt";
    rev = "v4.35.0";
    sha256 = "1f0g05sdgk5axvpwxi52vm54v0dflvy11gxp2sp7x6v49q01x24c";
  };
  isLibrary = false;
  meta.mainProgram = "lean-fmt";
}
