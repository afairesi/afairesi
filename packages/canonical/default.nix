{ inputs, pkgs, ... }:
let
  cosmicRay = python.pkgs.buildPythonPackage {
    build-system = [
      python.pkgs.hatch-vcs
      python.pkgs.hatchling
    ];
    dependencies = [
      exitCodes
    ]
    ++ (with python.pkgs; [
      aiohttp
      anybadge
      attrs
      click
      decorator
      gitpython
      parso
      rich
      sqlalchemy
      stevedore
      toml
      yattag
    ]);
    pname = "cosmic-ray";
    pyproject = true;
    pythonImportsCheck = [ "cosmic_ray.cli" ];
    src = pkgs.fetchurl {
      sha256 = "f1e8cdbd9b8c9d9145bc2b37247081b423d1e079d929a3da30714b23d40b4a70";
      url = "https://files.pythonhosted.org/packages/b7/44/0f033f653a859f82efb8b72edfa3dd3feba30f94b1784c74149908d9bbfd/cosmic_ray-8.7.0.tar.gz";
    };
    version = "8.7.0";
  };
  exitCodes = python.pkgs.buildPythonPackage {
    format = "wheel";
    pname = "exit-codes";
    src = pkgs.fetchurl {
      sha256 = "09444844772043f9be22128856088792be0f91cdbe269af992b1338a5464dc85";
      url = "https://files.pythonhosted.org/packages/fa/07/5dc359aba858ec096bd9e72c4a214617c95da24553a736bcce7818a61486/exit_codes-1.3.0-py2.py3-none-any.whl";
    };
    version = "1.3.0";
  };
  pname = baseNameOf ./.;
  python = pkgs.python3;
in
python.pkgs.buildPythonPackage {
  inherit pname;
  installPhase = ''
    install -Dm644 main.py "$out/${python.sitePackages}/$pname/__init__.py"
    mkdir -p "$out/bin"
    printf '%s\n' '#!${python.interpreter}' "from $pname import main" 'main()' > "$out/bin/$pname"
    chmod 755 "$out/bin/$pname"
    if [ -d prm ]; then
      cp -R prm/ "$out/${python.sitePackages}/$pname/"
    fi
  '';
  meta = {
    description = "Manage canonical persistent state in home and flake repositories";
    mainProgram = pname;
  };
  passthru = {
    checkInputs = [
      inputs.self.packages.${pkgs.stdenv.system}.nix_alphabetize
      inputs.self.packages.${pkgs.stdenv.system}.remove_empty_lines
      inputs.self.packages.${pkgs.stdenv.system}.uncomment
      pkgs.deadnix
      pkgs.nixfmt
      pkgs.statix
      python.pkgs.coverage
    ];
    python = python;
  };
  postFixup = ''
    touch "$out/${python.sitePackages}/$pname/py.typed"
  '';
  propagatedBuildInputs = [
    cosmicRay
    inputs.self.packages.${pkgs.stdenv.system}.nix_syntax
    pkgs.git
    pkgs.nix
  ];
  pyproject = false;
  src = ./.;
  strictDeps = true;
  version = "0.0.0";
}
