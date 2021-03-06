import logging
import os
import math
import json
import time
from typing import Sequence
from pathlib import Path

from dataclasses import dataclass

import casadi.casadi as cs
import numpy as np
import opengen as og

from smarts.core.agent import AgentPolicy
from smarts.core.coordinates import Heading
from smarts.core.utils import networking


VERSION = 175


def angle_error(a, b):
    return cs.fmin((a - b) ** 2.0, (a - (b + math.pi * 2.0)) ** 2.0)


@dataclass
class Gain:
    theta: cs.SXElem
    position: cs.SXElem
    obstacle: cs.SXElem
    u_accel: cs.SXElem
    u_yaw_rate: cs.SXElem
    terminal: cs.SXElem
    impatience: cs.SXElem
    speed: cs.SXElem
    DOF = 8

    def __iter__(self):
        return iter(
            [
                self.theta,
                self.position,
                self.obstacle,
                self.u_accel,
                self.u_yaw_rate,
                self.terminal,
                self.impatience,
                self.speed,
            ]
        )

    def setup_debug(self, plt):
        from matplotlib.widgets import Slider

        gains = [
            ("theta", self.theta, 0, 500),
            ("position", self.position, 0, 500),
            ("obstacle", self.obstacle, 0, 3000),
            ("u_accel", self.u_accel, 0, 500),
            ("u_yaw_rate", self.u_yaw_rate, 0, 500),
            ("terminal", self.terminal, 0, 10),
            ("impatience", self.impatience, 0, 5),
            ("speed", self.speed, 0, 5),
        ]
        self.debug_sliders = {}
        for i, (gain_name, gain_value, min_v, max_v) in enumerate(reversed(gains)):
            gain_axes = plt.axes([0.25, 0.03 * i, 0.65, 0.03])
            gain_slider = Slider(gain_axes, gain_name, min_v, max_v, valinit=gain_value)
            self.debug_sliders[gain_name] = gain_slider
            gain_slider.on_changed(self.update_debug)

    def update_debug(self, val=None):
        for gain_name, slider in self.debug_sliders.items():
            if gain_name == "theta":
                self.theta = slider.val
            elif gain_name == "position":
                self.position = slider.val
            elif gain_name == "obstacle":
                self.obstacle = slider.val
            elif gain_name == "u_accel":
                self.u_accel = slider.val
            elif gain_name == "u_yaw_rate":
                self.u_yaw_rate = slider.val
            elif gain_name == "terminal":
                self.terminal = slider.val
            elif gain_name == "impatience":
                self.impatience = slider.val
            elif gain_name == "speed":
                self.speed = slider.val

    def persist(self, file_path):
        gains = {
            "theta": self.theta,
            "position": self.position,
            "obstacle": self.obstacle,
            "u_accel": self.u_accel,
            "u_yaw_rate": self.u_yaw_rate,
            "terminal": self.terminal,
            "impatience": self.impatience,
            "speed": self.speed,
        }

        with open(file_path, "w") as fp:
            json.dump(gains, fp)

    @classmethod
    def load(cls, file_path):
        with open(file_path, "r") as fp:
            gains = json.load(fp)

        for gain_name, val in gains.items():
            if gain_name == "theta":
                theta = val
            elif gain_name == "position":
                position = val
            elif gain_name == "obstacle":
                obstacle = val
            elif gain_name == "u_accel":
                u_accel = val
            elif gain_name == "u_yaw_rate":
                u_yaw_rate = val
            elif gain_name == "terminal":
                terminal = val
            elif gain_name == "impatience":
                impatience = val
            elif gain_name == "speed":
                speed = val

        return Gain(
            theta=theta,
            position=position,
            obstacle=obstacle,
            u_accel=u_accel,
            u_yaw_rate=u_yaw_rate,
            terminal=terminal,
            impatience=impatience,
            speed=speed,
        )


@dataclass
class Number:
    value: cs.SXElem
    DOF = 1


@dataclass
class VehicleModel:
    """
    Based on the vehicle model defined here:
    http://planning.cs.uiuc.edu/node658.html
    """

    x: cs.SXElem
    y: cs.SXElem
    theta: cs.SXElem
    speed: cs.SXElem
    LENGTH = 4.0
    MAX_SPEED = 14.0  # m/s roughly 50km/h
    MAX_ACCEL = 5.0  # m/s/s
    DOF = 4

    @property
    def as_xref(self):
        return XRef(x=self.x, y=self.y, theta=self.theta)

    def step(self, u: "U", ts):
        self.x += ts * self.speed * cs.cos(self.theta)
        self.y += ts * self.speed * cs.sin(self.theta)
        self.theta += ts * self.speed / self.LENGTH * u.yaw_rate
        self.speed += ts * self.MAX_ACCEL * u.accel
        self.speed = cs.fmin(cs.fmax(0, self.speed), self.MAX_SPEED)


@dataclass
class XRef:
    x: cs.SXElem
    y: cs.SXElem
    theta: cs.SXElem
    DOF = 3

    def weighted_distance_to(self, other: "XRef", gain: Gain):
        theta_err = angle_error(self.theta, other.theta)
        pos_err = (other.x - self.x) ** 2 + (other.y - self.y) ** 2
        return gain.position * pos_err + gain.theta * theta_err


def min_cost_by_distance(xrefs: Sequence[XRef], point: XRef, gain: Gain):
    x_ref_iter = iter(xrefs)
    min_xref_t_cost = next(x_ref_iter).weighted_distance_to(point, gain)
    for xref_t in x_ref_iter:
        min_xref_t_cost = cs.fmin(
            min_xref_t_cost, xref_t.weighted_distance_to(point, gain),
        )

    return min_xref_t_cost


@dataclass
class U:
    accel: cs.SXElem
    yaw_rate: cs.SXElem


class UTrajectory:
    def __init__(self, N):
        self.N = N
        self.u = cs.SX.sym("u", 2 * N)

    @property
    def symbolic(self):
        return self.u

    def __getitem__(self, i):
        assert 0 <= i < self.N
        return U(accel=self.u[i * 2], yaw_rate=self.u[i * 2 + 1])

    def integration_cost(self, gain: Gain):
        cost = 0
        for t in range(1, self.N):
            prev_u_t = self[t - 1]
            u_t = self[t]
            cost += gain.u_accel * (u_t.accel - prev_u_t.accel) ** 2
            cost += gain.u_yaw_rate * (u_t.yaw_rate - prev_u_t.yaw_rate) ** 2

        return cost


class Policy(AgentPolicy):
    def __init__(
        self,
        N=11,
        SV_N=4,
        WP_N=15,
        ts=0.1,
        Q_theta=10,
        Q_position=10,
        Q_obstacle=100,
        Q_u_accel=10,
        Q_u_yaw_rate=4,
        Q_n=4,
        Q_impatience=1,
        Q_speed=0,
        debug=False,
        retries=5,
    ):
        self.log = logging.getLogger(self.__class__.__name__)
        self.debug = debug
        self.last_position = None
        self.steps_without_moving = 0
        self.N = N
        self.SV_N = SV_N
        self.WP_N = WP_N
        self.ts = ts
        self.gain_save_path = Path("gain.json")
        if self.gain_save_path.exists():
            print(f"Loading gains from {self.gain_save_path.absolute()}")
            self.gain = Gain.load(self.gain_save_path)
        else:
            self.gain = Gain(
                theta=Q_theta,
                position=Q_position,
                obstacle=Q_obstacle,
                u_accel=Q_u_accel,
                u_yaw_rate=Q_u_yaw_rate,
                terminal=Q_n,
                impatience=Q_impatience,
                speed=Q_speed,
            )
        self.retries = retries
        self.mng = None

        if self.debug:
            self._setup_debug_pannel()

        self.init_planner()

    def init_planner(self):
        self.prev_solution = None
        build_dir = "OpEn_build"
        planner_name = "trajectory_optimizer"
        build_mode = "release"  # use "debug" for faster compilation times

        planner_name = f"{planner_name}_v{VERSION}"
        versioned_params = [
            self.N,
            self.SV_N,
            self.WP_N,
            self.ts,
        ]
        params_str = "_".join(str(p) for p in versioned_params)
        build_dir = f"{build_dir}/{params_str}"
        path_to_planner = f"{build_dir}/{planner_name}"

        if not os.path.exists(path_to_planner):
            problem = self.build_problem()
            build_config = (
                og.config.BuildConfiguration()
                .with_build_directory(build_dir)
                .with_build_mode(build_mode)
                .with_tcp_interface_config()
            )
            meta = og.config.OptimizerMeta().with_optimizer_name(planner_name)
            solver_config = (
                og.config.SolverConfiguration()
                # TODO: hire interns to tune these values
                # .with_tolerance(1e-6)
                # .with_initial_tolerance(1e-4)
                # .with_max_outer_iterations(10)
                # .with_delta_tolerance(1e-2)
                # .with_penalty_weight_update_factor(10.0)
            )
            builder = og.builder.OpEnOptimizerBuilder(
                problem, meta, build_config, solver_config
            ).with_verbosity_level(1)
            builder.build()

        for i in range(self.retries):
            try:
                self.mng = og.tcp.OptimizerTcpManager(
                    path_to_planner, port=networking.find_free_port()
                )
                self.mng.start()
                break
            except Exception as e:
                self.log.warn(
                    f"Failed to start optimizer, attempt: {i + 1} / {self.retries}"
                )

        assert self.is_planner_running()

    def build_problem(self):
        # Assumptions
        assert self.N >= 2, f"Must generate at least 2 trajectory points, got: {self.N}"
        assert self.SV_N >= 0, f"Must have non-negative # of sv's, got: {self.SV_N}"
        assert self.WP_N >= 1, f"Must have at lest 1 trajectory reference"
        # TODO: rename WP_N to xref_n

        z0_schema = [
            (1, Gain),
            (1, VehicleModel),  # Ego
            (self.SV_N, VehicleModel),  # SV's
            (self.WP_N, XRef),  # reference trajectory
            (1, Number),  # impatience
            (1, Number),  # target_speed
        ]

        z0_dimensions = sum(n * feature.DOF for n, feature in z0_schema)
        z0 = cs.SX.sym("z0", z0_dimensions)
        u_traj = UTrajectory(self.N)

        # parse z0 into features
        position = 0
        parsed = []
        for n, feature, in z0_schema:
            feature_group = []
            for i in range(n):
                feature_group.append(
                    feature(*z0[position : position + feature.DOF].elements())
                )
                position += feature.DOF
            if n > 1:
                parsed.append(feature_group)
            else:
                assert len(feature_group) == 1
                parsed += feature_group

        assert position == len(z0.elements())
        assert position == z0_dimensions

        gain, ego, social_vehicles, xref_traj, impatience, target_speed = parsed

        cost = 0

        for t in range(self.N):
            # Integrate the ego vehicle forward to the next trajectory point
            ego.step(u_traj[t], self.ts)

            # For the current pose, compute the smallest cost to any xref
            cost += min_cost_by_distance(xref_traj, ego.as_xref, gain)

            cost += gain.speed * target_speed.value / t

            for sv in social_vehicles:
                # step the social vehicle assuming no change in velocity or heading
                sv.step(U(accel=0, yaw_rate=0), self.ts)

                min_dist = VehicleModel.LENGTH
                cost += gain.obstacle * cs.fmax(
                    -1, min_dist ** 2 - ((ego.x - sv.x) ** 2 + (ego.y - sv.y) ** 2),
                )

        # To stabilize the trajectory, we attach a higher weight to the final x_ref
        cost += gain.terminal * xref_traj[-1].weighted_distance_to(ego.as_xref, gain)

        cost += u_traj.integration_cost(gain)

        # force acceleration when we become increasingly impatient
        cost += gain.impatience * (
            (u_traj[0].accel - 1.0) * impatience.value ** 2 * -(1.0)
        )

        bounds = og.constraints.Rectangle(
            xmin=[-1, -math.pi * 0.3] * self.N, xmax=[1, math.pi * 0.3] * self.N
        )

        return og.builder.Problem(u_traj.symbolic, z0, cost).with_constraints(bounds)

    def is_planner_running(self) -> bool:
        return self.mng and self.mng._OptimizerTcpManager__check_if_server_is_running()

    def stop_planner(self):
        if self.is_planner_running():
            # If the manager has already been killed through other means
            self.mng.kill()

            while self.is_planner_running():
                # wait for the optimizer to die
                time.sleep(0.1)

    def reinit_planner(self):
        self.prev_solution = None
        self.stop_planner()
        self.init_planner()

    def __del__(self):
        self.stop_planner()

    def act(self, obs):
        ego = obs.ego_vehicle_state
        if (
            self.last_position is not None
            and np.linalg.norm(ego.position - self.last_position) < 1e-1
        ):
            self.steps_without_moving += 1
        else:
            self.steps_without_moving = 0

        wps = min(obs.waypoint_paths, key=lambda wps: wps[0].dist_to(ego.position))

        # drop the first few waypoint to get the vehicle to move
        wps_to_skip = 0  # TODO: remove this if we end up not fixing it at 0
        wps = wps[min(wps_to_skip, len(wps) - 1) : self.WP_N + wps_to_skip]

        # repeat the last waypoint to fill out self.WP_N waypoints
        wps += [wps[-1]] * (self.WP_N - len(wps))
        wps_params = [
            wp_param
            for wp in wps
            for wp_param in [wp.pos[0], wp.pos[1], float(wp.heading) + math.pi * 0.5]
        ]
        if self.SV_N == 0:
            sv_params = []
        elif len(obs.neighborhood_vehicle_states) == 0 and self.SV_N > 0:
            # We have no social vehicles in the scene, create placeholders far away
            sv_params = [
                ego.position[0] + 100000,
                ego.position[1] + 100000,
                0,
                0,
            ] * self.SV_N
        else:
            # Give the closest SV_N social vehicles to the planner
            social_vehicles = sorted(
                obs.neighborhood_vehicle_states,
                key=lambda sv: np.linalg.norm(sv.position - ego.position),
            )[: self.SV_N]

            # repeat the last social vehicle to ensure SV_N social vehicles
            social_vehicles += [social_vehicles[-1]] * (
                self.SV_N - len(social_vehicles)
            )
            sv_params = [
                sv_param
                for sv in social_vehicles
                for sv_param in [
                    sv.position[0],
                    sv.position[1],
                    float(sv.heading) + math.pi * 0.5,
                    sv.speed,
                ]
            ]

        ego_params = [
            ego.position[0],
            ego.position[1],
            float(ego.heading) + math.pi * 0.5,
            ego.speed,
        ]

        impatience = self.steps_without_moving
        planner_params = (
            list(self.gain)
            + ego_params
            + sv_params
            + wps_params
            + [impatience, wps[0].speed_limit]
        )

        resp = self.mng.call(planner_params, initial_guess=self.prev_solution)

        if resp.is_ok():
            u_star = resp["solution"]
            self.prev_solution = u_star
            ego_model = VehicleModel(*ego_params)
            xs = []
            ys = []
            headings = []
            speeds = []
            for u in zip(u_star[::2], u_star[1::2]):
                ego_model.step(U(*u), self.ts)
                headings.append(Heading(ego_model.theta - math.pi * 0.5))
                xs.append(ego_model.x)
                ys.append(ego_model.y)
                speeds.append(ego_model.speed)

            traj = [xs, ys, headings, speeds]
            if self.debug:
                self._draw_debug_panel(xs, ys, wps, sv_params, ego, u_star)

            act = traj
        else:
            print("Bad resp. from planner:", resp.get().code)
            # re-init the planner and stay still, hopefully once we've re-initialized, we can recover
            self.reinit_planner()
            act = None

        self.last_position = ego.position
        return act

    def _setup_debug_pannel(self):
        import matplotlib.pyplot as plt

        self.plt = plt
        self.plt.close()  # close any open plts from previous episodes
        self.gain.setup_debug(plt)
        self.plt.ion()

    def _draw_debug_panel(self, xs, ys, wps, sv_params, ego, u_star):
        self.gain.persist(self.gain_save_path)

        subplot = self.plt.subplot(221)
        subplot.clear()

        self.plt.plot(xs, ys, "o-", color="xkcd:crimson", label="trajectory")
        wp_x = [wp.pos[0] for wp in wps]
        wp_y = [wp.pos[1] for wp in wps]
        self.plt.scatter(wp_x, wp_y, color="red", label="waypoint")

        sv_x = [sv_x for sv_x in sv_params[:: VehicleModel.DOF]]
        sv_y = [sv_y for sv_y in sv_params[1 :: VehicleModel.DOF]]
        self.plt.scatter(sv_x, sv_y, label="social vehicles")
        plt_radius = 50
        self.plt.axis(
            (
                ego.position[0] - plt_radius,
                ego.position[0] + plt_radius,
                ego.position[1] - plt_radius,
                ego.position[1] + plt_radius,
            )
        )
        self.plt.legend()

        subplot = self.plt.subplot(222)
        subplot.clear()
        u_accels = u_star[::2]
        u_thetas = u_star[1::2]
        ts = range(len(u_accels))
        self.plt.plot(ts, u_accels, "o-", color="gold", label="u_accel")
        self.plt.plot(ts, u_thetas, "o-", color="purple", label="u_theta")
        self.plt.legend()

        self.plt.draw()
        self.plt.pause(1e-6)
