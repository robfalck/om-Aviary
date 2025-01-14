import openmdao.api as om
from openmdao.utils.assert_utils import assert_near_equal

from aviary.interface.methods_for_level2 import AviaryGroup
from aviary.mission.gasp_based.phases.time_integration_traj import FlexibleTraj
from aviary.mission.flops_based.phases.time_integration_phases import \
    SGMHeightEnergy, SGMDetailedTakeoff, SGMDetailedLanding
from aviary.subsystems.premission import CorePreMission
from aviary.utils.functions import set_aviary_initial_values
from aviary.variable_info.enums import Verbosity, EquationsOfMotion
from aviary.variable_info.variables import Aircraft, Dynamic, Mission, Settings
from aviary.variable_info.variables_in import VariablesIn

from aviary.interface.default_phase_info.height_energy import aero, prop, geom
from aviary.subsystems.propulsion.engine_deck import EngineDeck
from aviary.utils.process_input_decks import create_vehicle
from aviary.utils.preprocessors import preprocess_propulsion
from aviary.variable_info.variable_meta_data import _MetaData as BaseMetaData

import warnings
import unittest
import importlib


@unittest.skipUnless(importlib.util.find_spec("pyoptsparse") is not None, "pyoptsparse is not installed")
class HE_SGMDescentTestCase(unittest.TestCase):
    def setUp(self):
        aviary_inputs, initial_guesses = create_vehicle(
            'models/test_aircraft/aircraft_for_bench_FwFm.csv')
        aviary_inputs.set_val(Aircraft.Engine.SCALED_SLS_THRUST, val=28690, units="lbf")
        aviary_inputs.set_val(Dynamic.Mission.THROTTLE, val=0, units="unitless")
        aviary_inputs.set_val(Mission.Takeoff.ROLLING_FRICTION_COEFFICIENT,
                              val=0.0175, units="unitless")
        aviary_inputs.set_val(Mission.Takeoff.BRAKING_FRICTION_COEFFICIENT,
                              val=0.35, units="unitless")
        aviary_inputs.set_val(Settings.EQUATIONS_OF_MOTION, val=EquationsOfMotion.SOLVED)
        ode_args = dict(aviary_options=aviary_inputs, core_subsystems=[prop, geom, aero])
        preprocess_propulsion(aviary_inputs, [EngineDeck(options=aviary_inputs)])

        ode_args['num_nodes'] = 1
        ode_args['subsystem_options'] = {'core_aerodynamics': {'method': 'computed'}}

        self.ode_args = ode_args
        self.aviary_inputs = aviary_inputs
        self.tol = 1e-5

    def setup_prob(self, phases) -> om.Problem:
        prob = om.Problem()
        prob.driver = om.pyOptSparseDriver()
        prob.driver.options["optimizer"] = 'IPOPT'
        prob.driver.opt_settings['tol'] = 1.0E-6
        prob.driver.opt_settings['mu_init'] = 1e-5
        prob.driver.opt_settings['max_iter'] = 50
        prob.driver.opt_settings['print_level'] = 5

        aviary_options = self.ode_args['aviary_options']
        subsystems = self.ode_args['core_subsystems']

        traj = FlexibleTraj(
            Phases=phases,
            promote_all_auto_ivc=True,
            traj_final_state_output=[Dynamic.Mission.MASS,
                                     Dynamic.Mission.DISTANCE,
                                     Dynamic.Mission.ALTITUDE],
            traj_initial_state_input=[
                Dynamic.Mission.MASS,
                Dynamic.Mission.DISTANCE,
                Dynamic.Mission.ALTITUDE,
            ],
        )
        prob.model = AviaryGroup(aviary_options=aviary_options,
                                 aviary_metadata=BaseMetaData)
        prob.model.add_subsystem(
            'pre_mission',
            CorePreMission(aviary_options=aviary_options,
                           subsystems=subsystems),
            promotes_inputs=['aircraft:*', 'mission:*'],
            promotes_outputs=['aircraft:*', 'mission:*']
        )
        prob.model.add_subsystem('traj', traj,
                                 promotes=['aircraft:*', 'mission:*']
                                 )

        prob.model.add_subsystem(
            "fuel_obj",
            om.ExecComp(
                "reg_objective = overall_fuel/10000",
                reg_objective={"val": 0.0, "units": "unitless"},
                overall_fuel={"units": "lbm"},
            ),
            promotes_inputs=[
                ("overall_fuel", Mission.Summary.TOTAL_FUEL_MASS),
            ],
            promotes_outputs=[("reg_objective", Mission.Objectives.FUEL)],
        )

        prob.model.add_objective(Mission.Objectives.FUEL, ref=1e4)

        prob.model.add_subsystem(
            'input_sink',
            VariablesIn(aviary_options=self.aviary_inputs,
                        meta_data=BaseMetaData),
            promotes_inputs=['*'],
            promotes_outputs=['*'])

        with warnings.catch_warnings():

            # Set initial default values for all LEAPS aircraft variables.
            set_aviary_initial_values(
                prob.model, self.aviary_inputs, meta_data=BaseMetaData)

            warnings.simplefilter("ignore", om.PromotionWarning)

            prob.setup()

        return prob

    def run_simulation(self, phases, initial_values: dict):
        prob = self.setup_prob(phases)

        for key, val in initial_values.items():
            prob.set_val(key, **val)

        prob.run_model()

        distance = prob.get_val('traj.distance_final', units='NM')[0]
        mass = prob.get_val('traj.mass_final', units='lbm')[0]
        alt = prob.get_val('traj.altitude_final', units='ft')[0]

        final_states = {'distance': distance, 'mass': mass, 'altitude': alt}
        return final_states

    # def test_takeoff(self):
    #     initial_values_takeoff = {
    #         "traj.altitude_initial": {'val': 0, 'units': "ft"},
    #         "traj.mass_initial": {'val': 171000, 'units': "lbm"},
    #         "traj.distance_initial": {'val': 0, 'units': "NM"},
    #         "traj.velocity": {'val': .1, 'units': "m/s"},
    #     }

    #     ode_args = self.ode_args
    #     ode_args['friction_key'] = Mission.Takeoff.ROLLING_FRICTION_COEFFICIENT
    #     brake_release_to_decision = SGMDetailedTakeoff(
    #         ode_args,
    #         simupy_args=dict(verbosity=Verbosity.DEBUG,)
    #         )
    #     brake_release_to_decision.clear_triggers()
    #     brake_release_to_decision.add_trigger(Dynamic.Mission.VELOCITY, value=167.85, units='kn')

    #     phases = {'HE': {
    #         'ode': brake_release_to_decision,
    #         'vals_to_set': {}
    #     }}

    #     final_states = self.run_simulation(phases, initial_values_takeoff)
    #     # assert_near_equal(final_states['altitude'], 500, self.tol)
    #     assert_near_equal(final_states['velocity'], 167.85, self.tol)

    def test_cruise(self):
        initial_values_cruise = {
            "traj.altitude_initial": {'val': 35000, 'units': "ft"},
            "traj.mass_initial": {'val': 171000, 'units': "lbm"},
            "traj.distance_initial": {'val': 0, 'units': "NM"},
            "traj.mach": {'val': .8, 'units': "unitless"},
        }

        SGMCruise = SGMHeightEnergy(
            self.ode_args,
            phase_name='cruise',
            simupy_args=dict(verbosity=Verbosity.QUIET,)
        )
        SGMCruise.triggers[0].value = 160000

        phases = {'HE': {
            'ode': SGMCruise,
            'vals_to_set': {}
        }}

        final_states = self.run_simulation(phases, initial_values_cruise)
        assert_near_equal(final_states['mass'], 160000, self.tol)

    # def test_landing(self):
    #     initial_values_landing = {
    #         "traj.altitude_initial": {'val': 35000, 'units': "ft"},
    #         "traj.mass_initial": {'val': 171000, 'units': "lbm"},
    #         "traj.distance_initial": {'val': 0, 'units': "NM"},
    #         "traj.velocity": {'val': 300, 'units': "m/s"},
    #     }

    #     ode_args = self.ode_args
    #     ode_args['friction_key'] = Mission.Takeoff.BRAKING_FRICTION_COEFFICIENT
    #     phases = {'HE': {
    #         'ode': SGMDetailedLanding(
    #             ode_args,
    #             simupy_args=dict(verbosity=Verbosity.QUIET,)
    #         ),
    #         'vals_to_set': {}
    #     }}

    #     final_states = self.run_simulation(phases, initial_values_landing)
    #     assert_near_equal(final_states['altitude'], 0, self.tol)


if __name__ == '__main__':
    unittest.main()
