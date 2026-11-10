{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
  };

  outputs = {
    self,
    nixpkgs,
  }: let
    system = "x86_64-linux";

    pkgs = import nixpkgs {
      inherit system;
    };

    python = pkgs.python3.withPackages (ps:
      with ps; [
        wget
        requests
        pandas
        lxml
        beautifulsoup4
        python-telegram-bot
        apscheduler
      ]);
  in {
    devShells.${system}.default = pkgs.mkShell {
      buildInputs = [
        python
        pkgs.cacert
      ];
    };
  };
}
