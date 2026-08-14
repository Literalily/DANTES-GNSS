import os
import sys
import subprocess
import venv 


def get_project_paths():
    # Dynamically resolves paths relative to where script is saved (portability)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    default_venv = os.path.join(script_dir, "venvGNSSProject")
    return script_dir, default_venv

def ensure_virtual_environment(venv_dir):
    # Verifies if the required virtual environment exists; if not, builds it
    # and installs dependencies from requiremnts.txt
    
    if os.path.exists(venv_dir):
        print("\n" + "=" * 60)
        print("          GNSS PROJECT WORKSPACE SETUP           ")
        print("=" * 60)
        print(f"Notice: Local environment 'venvGNSSProject' already installed at:\n{venv_dir}")
        print("=" * 60)
        input("Press any button to exit: ").strip().lower()
        return True  # environment exists thumbs up

    print("\n" + "=" * 60)
    print("          GNSS PROJECT WORKSPACE SETUP           ")
    print("=" * 60)
    print(f"Notice: Local environment 'venvGNSSProject' was not found at:\n{venv_dir}")
    print("\nThe system can automatically construct this environment and install")
    print("the required dependencies.")
    print("=" * 60)

    confirm = input("Would you like to run the automatic installation now? (y/n): ").strip().lower()
    if confirm not in ['y', 'yes']:
        print("Setup cancelled. The project cannot run without its environment.")
        return False

    try:
        print("\n[1/2] Initializing local virtual environment...")
        venv.create(venv_dir, with_pip=True)

        pip_exe = os.path.join(venv_dir, "Scripts", "pip.exe")
        requirements_path = os.path.join(os.path.dirname(venv_dir), "requirements.txt")

        print("\n[2/2] Installing dependencies from requirements.txt...")
        subprocess.run([pip_exe, "install", "-r", requirements_path], check=True)

        print("\n" + "=" * 60)
        print("SUCCESS: Virtual environment created and dependencies installed.")
        print("=" * 60 + "\n")
        return True

    except Exception as e:
        print(f"\nCRITICAL ENVIRONMENT INITIALIZATION FAILURE: {e}")
        print("Please check your network link or disk permissions, delete the partial folder, and restart.")
        return False


def run_pipeline():
    script_dir, venv_dir = get_project_paths()

    if not ensure_virtual_environment(venv_dir):
        input("\nPress Enter to exit...")
        sys.exit(1)


if __name__ == "__main__":
    run_pipeline()