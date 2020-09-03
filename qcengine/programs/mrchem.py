"""
Calls the MRChem executable.
"""
import copy
import json
import sys
from pathlib import Path
import pprint
import logging
from typing import TYPE_CHECKING, Dict, Any
from collections import Counter

import numpy as np
from qcelemental.models import AtomicResult
from qcelemental.util import parse_version, safe_version, which
from qcelemental.molparse import from_schema
from qcelemental.molparse.to_string import _atoms_formatter

from ..exceptions import InputError, RandomError, UnknownError
from ..util import execute, popen, temporary_directory, create_mpi_invocation
from .model import ProgramHarness

if TYPE_CHECKING:
    from qcelemental.models import AtomicInput

    from ..config import TaskConfig

pp = pprint.PrettyPrinter(width=120, compact=True, indent=1)
logger = logging.getLogger(__name__)


class MRChemHarness(ProgramHarness):

    _defaults = {
        "name": "MRChem",
        "scratch": False,
        "thread_safe": False,
        "thread_parallel": True,
        "node_parallel": True,
        "managed_memory": True,
    }
    version_cache: Dict[str, str] = {}

    class Config(ProgramHarness.Config):
        pass

    @staticmethod
    def found(raise_error: bool = False) -> bool:
        """Whether MRChem harness is ready for operation.

        Parameters
        ----------
        raise_error: bool
            Passed on to control negative return between False and ModuleNotFoundError raised.

        Returns
        -------
        bool
            If mrchem and mrchem.x are found, returns True.
            If raise_error is False and MRCHem is missing, returns False.
            If raise_error is True and MRChem is missing, the error message is raised.

        """
        mrchem_x = which(
            "mrchem.x",
            return_bool=True,
            raise_error=raise_error,
            raise_msg="Please install via https://mrchem.readthedocs.io",
        )
        mrchem = which(
            "mrchem",
            return_bool=True,
            raise_error=raise_error,
            raise_msg="Please install via https://mrchem.readthedocs.io",
        )

        return mrchem and mrchem_x

    def get_version(self) -> str:
        self.found(raise_error=True)

        which_prog = which("mrchem.x")
        if which_prog not in self.version_cache:
            with popen([which_prog, "--version"]) as exc:
                exc["proc"].wait(timeout=30)
            self.version_cache[which_prog] = safe_version(exc["stdout"].split()[-1])

        candidate_version = self.version_cache[which_prog]

        return candidate_version

    def compute(self, input_model: "AtomicInput", config: "TaskConfig") -> "AtomicResult":
        """
        Runs MRChem in executable mode
        """
        self.found(raise_error=True)

        # Location resolution order config.scratch_dir, /tmp
        parent = config.scratch_directory

        error_type = None
        error_message = None
        compute_success = False

        job_input = self.build_input(input_model, config)
        input_data = copy.deepcopy(job_input["mrchem_json"])
        output_data = {
            "keywords": input_data,
            "schema_name": "qcschema_output",
            "schema_version": 1,
            "model": input_model.model,
            "molecule": input_model.molecule,
            "properties": {},
        }

        with temporary_directory(parent=parent, suffix="_mrchem_scratch") as tmpdir:
            # create folders
            for d in job_input["folders"]:
                if not Path(d).exists():
                    Path(d).mkdir()

            # Execute the program
            success, output = execute(
                command=job_input["command"] + ["data.json"],
                infiles={"data.json": json.dumps(job_input["mrchem_json"])},
                outfiles=["data.json"],
                scratch_directory=tmpdir,
            )

            if success:
                output_data["stdout"] = output["stdout"]
                # get data from the MRChem JSON output and transfer it to the QCSchema output
                mrchem_output = json.loads(output["outfiles"]["data.json"])["output"]
                output_data["success"] = mrchem_output["success"]
                output_data["driver"] = input_model.driver
                # Fill up properties
                occs = Counter(mrchem_output["properties"]["orbital_energies"]["spin"])
                output_data["properties"] = {
                    "calcinfo_nmo": len(mrchem_output["properties"]["orbital_energies"]["occupation"]),
                    "calcinfo_nalpha": occs["p"] + occs["a"],
                    "calcinfo_nbeta": occs["p"] + occs["b"],
                    "calcinfo_natom": len(input_model.molecule.masses),
                    "return_energy": mrchem_output["properties"]["scf_energy"]["E_tot"],
                    "scf_one_electron_energy":
                       mrchem_output["properties"]["scf_energy"]["E_kin"] +
                       mrchem_output["properties"]["scf_energy"]["E_en"] +
                       mrchem_output["properties"]["scf_energy"]["E_next"] +
                       mrchem_output["properties"]["scf_energy"]["E_eext"],
                    "scf_two_electron_energy":
                       mrchem_output["properties"]["scf_energy"]["E_ee"] +
                       mrchem_output["properties"]["scf_energy"]["E_x"] +
                       mrchem_output["properties"]["scf_energy"]["E_xc"],
                    "nuclear_repulsion_energy": mrchem_output["properties"]["scf_energy"]["E_nn"],
                    "scf_xc_energy": mrchem_output["properties"]["scf_energy"]["E_xc"],
                    "scf_total_energy": mrchem_output["properties"]["scf_energy"]["E_tot"],
                    "scf_iterations": len(mrchem_output["scf_calculation"]["scf_solver"]["cycles"]),
                }

                if input_model.driver == "energy":
                    output_data["return_result"] = mrchem_output["properties"]["scf_energy"]["E_tot"]
                elif input_model.driver == "properties":
                    # we probably want a loop over properties known to MRChem here
                    output_data["return_result"] = mrchem_output["properties"]["dipole_moment"]["dip-1"]["vector"]
                    output_data["properties"]["scf_dipole_moment"] = mrchem_output["properties"]["dipole_moment"]["dip-1"]["vector"]
                else:
                    raise RuntimeError(f"MRChem cannot run with {input_model.driver} driver")


                compute_success = mrchem_output["success"]

            else:
                output_data["stderr"] = output["stderr"]
                output_data["error"] = {
                    "error_message": output["stderr"],
                    "error_type": "execution_error",
                }

        # Dispatch errors, PSIO Errors are not recoverable for future runs
        if compute_success is False:

            if ("SIGSEV" in error_message) or ("SIGSEGV" in error_message) or ("segmentation fault" in error_message):
                raise RandomError(error_message)
            else:
                raise UnknownError(error_message)

        output_data["provenance"] = mrchem_output["provenance"]
        output_data["provenance"]["memory"] = round(config.memory, 3)

        return AtomicResult(**output_data)

    def build_input(self, input_model: "AtomicInput", config: "TaskConfig") -> Dict[str, Any]:
        with popen([which("mrchem"), "--module"]) as exc:
            exc["proc"].wait(timeout=30)
        sys.path.append(exc["stdout"].split()[-1])
        from mrchem import validate, translate_input

        mrchemrec = {
            "scratch_directory": config.scratch_directory,
        }

        opts = copy.deepcopy(input_model.keywords)

        # Handle molecule
        # TODO move to qcelemental
        # How to handle units? units = opts.get("world_units", "bohr")
        # 'Molecule': {'charge': 0, 'multiplicity': 1, 'translate': False, 'coords': 'O       0.0000  0.0000  -0.1250\nH      -1.4375  0.0000   1.0250\nH       1.4375  0.0000   1.0250\n'},
        atom_format = "{elem}"
        ghost_format = "@{elem}"
        molrec = from_schema(input_model.molecule.dict(), nonphysical=True)
        geom = np.asarray(molrec["geom"]).reshape((-1, 3))
        atoms = _atoms_formatter(molrec, geom, atom_format, ghost_format, width=7, prec=12, sp=2)
        opts["Molecule"] = {
            "charge": int(molrec["molecular_charge"]),
            "multiplicity": molrec["molecular_multiplicity"],
            "translate": molrec["fix_com"],
            "coords": "\n".join(atoms),
        }

        if "WaveFunction" in opts.keys():
            opts["WaveFunction"]["method"] = input_model.model.method
        else:
            opts["WaveFunction"] = {"method": input_model.model.method}
        # Log the job settings as constructed from the input model
        logger.debug("JOB_OPTS from InputModel")
        logger.debug(pp.pformat(opts))

        try:
            opts = validate(ir_in=opts)
        except Exception as e:
            raise InputError(f"Failure preparing input to MRChem\n {str(e)}")
        # Log the validated job settings
        logger.debug("JOB_OPTS after validation")
        logger.debug(pp.pformat(opts))
        mrchemrec["folders"] = [
            opts["SCF"]["path_checkpoint"],
            opts["SCF"]["path_orbitals"],
            opts["Response"]["path_checkpoint"],
            opts["Response"]["path_orbitals"],
            opts["Plotter"]["path"],
        ]

        try:
            opts = translate_input(opts)
        except Exception as e:
            raise InputError(f"Failure preparing input to MRChem\n {str(e)}")
        opts["printer"]["file_name"] = "data.inp"
        # Log the final job settings
        logger.debug("JOB_OPTS after translation")
        logger.debug(pp.pformat(opts))

        mrchemrec["mrchem_json"] = {
            "input": opts,
        }

        # Determine the command
        if config.use_mpiexec:
            mrchemrec["command"] = create_mpi_invocation(which("mrchem.x"), config)
            logger.info(f"Launching with mpiexec: {' '.join(mrchemrec['command'])}")
        else:
            mrchemrec["command"] = [which("mrchem.x")]

        return mrchemrec
