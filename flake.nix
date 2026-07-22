{
  description = "Convert Wise and Revolut USD statements into sevDesk-importable EUR CSV";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    git-hooks.url = "github:cachix/git-hooks.nix";
    git-hooks.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = { self, nixpkgs, git-hooks }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
      systemOf = pkgs: pkgs.stdenv.hostPlatform.system;
    in
    {
      packages = forAllSystems (pkgs: rec {
        default = sevdesk-importer;
        sevdesk-importer = pkgs.python3.pkgs.buildPythonApplication {
          pname = "sevdesk-importer";
          version = "1.0.0";
          format = "pyproject";
          src = ./.;
          nativeBuildInputs = [ pkgs.python3.pkgs.setuptools ];
          # tzdata carries the Europe/Vienna rules zoneinfo reads at runtime.
          propagatedBuildInputs = [ pkgs.python3.pkgs.tzdata ];
          # The test suite lives outside the installed package.
          doCheck = false;
          meta = {
            description = "Wise/Revolut USD statement to sevDesk EUR CSV converter";
            mainProgram = "sevdesk-importer";
          };
        };
      });

      apps = forAllSystems (pkgs: rec {
        default = sevdesk-importer;
        sevdesk-importer = {
          type = "app";
          program = nixpkgs.lib.getExe self.packages.${systemOf pkgs}.sevdesk-importer;
        };
      });

      checks = forAllSystems (pkgs: {
        pre-commit = git-hooks.lib.${systemOf pkgs}.run {
          src = ./.;
          hooks.ruff-format.enable = true;
        };
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          # Installs the git hook on entering the shell.
          inherit (self.checks.${systemOf pkgs}.pre-commit) shellHook;
          packages = [
            (pkgs.python3.withPackages (ps: [ ps.pytest ps.mypy ps.tzdata ]))
            pkgs.ruff
            pkgs.just
          ] ++ self.checks.${systemOf pkgs}.pre-commit.enabledPackages;
        };
      });
    };
}
