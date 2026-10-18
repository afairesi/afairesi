{
  enableMypy ? true,
  flake,
  inputs,
  pkgs,
  self ? inputs.self,
  ...
}:
let
  formatterPython = pkgs.python3.withPackages (_: [
    inputs.self.packages.${pkgs.stdenv.system}.afairesi
  ]);
  packagesPath = self.outPath + "/packages";
  pythonPackageNames =
    if enableMypy then
      builtins.attrNames (
        pkgs.lib.filterAttrs (
          name: type: type == "directory" && builtins.pathExists (packagesPath + "/${name}/main.py")
        ) (if builtins.pathExists packagesPath then builtins.readDir packagesPath else { })
      )
    else
      [ ];
  rawFormatter = treefmtEval.config.build.wrapper;
  treefmtEval = inputs.treefmt-nix.lib.evalModule pkgs {
    programs = {
      actionlint = {
        enable = true;
        includes = [
          ".github/workflows/workflow.yml"
          ".forgejo/workflows/workflow.yml"
        ];
        priority = 6;
      };
      deadnix = {
        enable = true;
        includes = [
          "flake.nix"
          "formatter.nix"
          "packages/*/default.nix"
          "hosts/*/configuration.nix"
          "hosts/*/hardware-configuration.nix"
        ];
        priority = 3;
      };
      mypy = {
        directories = pkgs.lib.genAttrs pythonPackageNames (
          name:
          let
            environment = inputs.self.lib.mkTestEnvironment {
              inherit pkgs;
              packageDrv = self.packages.${pkgs.stdenv.system}.${name};
            };
          in
          {
            directory = "";
            modules = [ "packages/${name}" ];
            options = [
              "--python-executable=${environment.python}/bin/python"
              "--cache-dir=packages/${name}/tmp/.mypy_cache"
              "--exclude=/(prm|tmp)/"
              "--explicit-package-bases"
              "--ignore-missing-imports"
              "--strict"
            ];
          }
        );
        enable = enableMypy;
      };
      nixfmt = {
        enable = true;
        includes = [
          "flake.nix"
          "formatter.nix"
          "packages/*/default.nix"
          "hosts/*/configuration.nix"
          "hosts/*/hardware-configuration.nix"
        ];
        priority = 5;
      };
      oxfmt = {
        enable = true;
        includes = [
          ".github/workflows/workflow.yml"
          ".forgejo/workflows/workflow.yml"
          "packages/*/style.css"
          "packages/*/index.html"
          "packages/*/script.js"
        ];
        priority = 5;
      };
      ruff-check = {
        enable = true;
        extendSelect = [ "ALL" ];
        includes = [
          "packages/*/main.py"
          "packages/*/test_main.py"
        ];
        priority = 3;
      };
      ruff-format = {
        enable = true;
        includes = [
          "packages/*/main.py"
          "packages/*/test_main.py"
        ];
        priority = 5;
      };
      statix = {
        enable = true;
        includes = [
          "flake.nix"
          "formatter.nix"
          "packages/*/default.nix"
          "hosts/*/configuration.nix"
          "hosts/*/hardware-configuration.nix"
        ];
        priority = 3;
      };
      texfmt = {
        enable = true;
        includes = [ "packages/*/ms.tex" ];
        priority = 5;
      };
      yamllint = {
        enable = true;
        includes = [
          ".github/workflows/workflow.yml"
          ".forgejo/workflows/workflow.yml"
        ];
        priority = 6;
      };
    };
    projectRootFile = "flake.nix";
    settings = {
      formatter = {
        bibtex-tidy = {
          command = pkgs.bibtex-tidy;
          includes = [ "packages/*/ms.bib" ];
          options = [
            "--duplicates"
            "--no-align"
            "--no-wrap"
            "--sort"
            "--sort-fields"
            "--v2"
          ];
          priority = 5;
        };
        nix-alphabetize = {
          command = inputs.self.packages.${pkgs.stdenv.system}.nix_alphabetize;
          includes = [
            "flake.nix"
            "formatter.nix"
            "packages/*/default.nix"
            "hosts/*/configuration.nix"
            "hosts/*/hardware-configuration.nix"
          ];
          priority = 4;
        };
        oxlint = {
          command = pkgs.oxlint;
          includes = [ "packages/*/script.js" ];
          options = [
            "--fix-dangerously"
            "-D"
            "all"
          ];
          priority = 3;
        };
        remove-empty-lines = {
          command = inputs.self.packages.${pkgs.stdenv.system}.remove_empty_lines;
          includes = [
            ".gitignore"
            "LICENSE"
            "flake.lock"
          ];
          priority = 2;
        };
        ruff-check.options = [
          "--cache-dir=tmp/.ruff_cache"
          "--per-file-ignores=test_main.py:INP001"
          "--unsafe-fixes"
        ];
        ruff-format.options = [ "--cache-dir=tmp/.ruff_cache" ];
        uncomment = {
          command = inputs.self.packages.${pkgs.stdenv.system}.uncomment;
          includes = [
            ".gitignore"
            "LICENSE"
            "flake.lock"
            "flake.nix"
            "formatter.nix"
            "packages/*/default.nix"
            "hosts/*/configuration.nix"
            "hosts/*/hardware-configuration.nix"
            "packages/*/main.py"
            "packages/*/test_main.py"
            "packages/*/index.html"
            "packages/*/script.js"
            "packages/*/style.css"
            "packages/*/ms.tex"
            "packages/*/ms.bib"
            ".github/workflows/workflow.yml"
            ".forgejo/workflows/workflow.yml"
          ];
          priority = 1;
        };
      }
      // pkgs.lib.genAttrs (map (name: "mypy-${name}") pythonPackageNames) (_: {
        priority = 6;
      });
      global.excludes = [ "{prm,tmp,*/prm,*/tmp}/**" ];
    };
  };
  wrapper = pkgs.writeShellApplication {
    name = "treefmt";
    runtimeInputs = [
      pkgs.git
      pkgs.nix
    ];
    text = ''
      ${formatterPython}/bin/python -c 'from afairesi import formatter_main; formatter_main()'
      exec ${rawFormatter}/bin/treefmt "$@"
    '';
  };
in
wrapper
// {
  passthru = wrapper.passthru // {
    raw = rawFormatter;
    tests.check = treefmtEval.config.build.check flake;
  };
}
