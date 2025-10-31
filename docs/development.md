# Development Environment Setup

Follow these steps to prepare a local environment for the Met DSL emission CLI.

0. **Install system dependencies (Ubuntu 22.04)** 

  
```bash 
   sudo apt update 
   sudo apt install python3-typer python3-pydantic python3-jinja2 python3-rich python3-yaml
   sudo apt install python3-pytest python3-ruff
  
``` 

1. **Create a virtual environment**

  
```bash
   python -m venv .venv
   source .venv/bin/activate
  
```

2. **Install project with development extras**

  
```bash
   pip install -e .[dev]
  
```

3. **Run linters and tests**

  
```bash
   ruff check .
   black --check src tests
   pytest
  
```

4. **Install Fortran toolchains as required**

   Ensure `gfortran`, Intel oneAPI Fortran, and NVIDIA NVFortran are available on `PATH` before running verification tasks.
5. **Smoke test the CLI scaffold**

  
```bash
   metdsl emit path/to/model.dsl --stage ir --config path/to/config.yaml
   metdsl list-targets
  
```
