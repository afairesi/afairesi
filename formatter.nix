{
  flake,
  inputs,
  pkgs,
  self ? inputs.self,
  ...
}:
let
  packagesPath = self.outPath + "/packages";
  pythonPackageNames = builtins.attrNames (
    pkgs.lib.filterAttrs (
      name: type: type == "directory" && builtins.pathExists (packagesPath + "/${name}/main.py")
    ) (if builtins.pathExists packagesPath then builtins.readDir packagesPath else { })
  );
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
          "checks/*/default.nix"
          "hosts/*/configuration.nix"
          "hosts/*/hardware-configuration.nix"
        ];
        priority = 3;
      };
      mypy = {
        directories = pkgs.lib.genAttrs pythonPackageNames (
          name:
          let
            package = self.packages.${pkgs.stdenv.system}.${name};
          in
          {
            directory = "";
            extraPythonPackages =
              (package.propagatedBuildInputs or [ ])
              ++ (package.buildInputs or [ ])
              ++ (package.checkInputs or [ ])
              ++ (package.nativeCheckInputs or [ ])
              ++ [
                pkgs.python3.pkgs.hypothesis
                pkgs.python3.pkgs.pytest
              ];
            modules = [ "packages/${name}" ];
            options = [
              "--cache-dir=/tmp/.mypy_cache/${name}"
              "--exclude=/(prm|tmp)/"
              "--explicit-package-bases"
              "--ignore-missing-imports"
              "--strict"
            ];
          }
        );
        enable = true;
      };
      nixfmt = {
        enable = true;
        includes = [
          "flake.nix"
          "formatter.nix"
          "packages/*/default.nix"
          "checks/*/default.nix"
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
          "checks/*/default.nix"
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
            "checks/*/default.nix"
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
          excludes = [ "README" ];
          includes = [
            ".gitignore"
            "LICENSE"
            "README"
            "flake.lock"
            "flake.nix"
            "formatter.nix"
            "packages/*/default.nix"
            "checks/*/default.nix"
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
          priority = 2;
        };
        remove-new-lines = {
          command = inputs.self.packages.${pkgs.stdenv.system}.remove_new_lines;
          includes = [
            "packages/*/style.css"
            "packages/*/index.html"
            "packages/*/script.js"
          ];
          priority = 2;
        };
        ruff-check.options = [
          "--cache-dir=/tmp/.ruff_cache"
          "--per-file-ignores=test_main.py:INP001"
          "--unsafe-fixes"
        ];
        ruff-format.options = [ "--cache-dir=/tmp/.ruff_cache" ];
        uncomment = {
          command = inputs.self.packages.${pkgs.stdenv.system}.uncomment;
          includes = [
            ".gitignore"
            "LICENSE"
            "README"
            "flake.lock"
            "flake.nix"
            "formatter.nix"
            "packages/*/default.nix"
            "checks/*/default.nix"
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
    runtimeInputs = [ inputs.self.packages.${pkgs.stdenv.system}.canonical ];
    text = ''
      canonical converge
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
