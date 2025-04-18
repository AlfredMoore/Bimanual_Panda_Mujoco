from collections import OrderedDict
from typing import Tuple

import click
import mujoco
import mujoco.viewer
import numpy as np
import robosuite
from robocasa.models.scenes.scene_registry import LayoutType, StyleType
from robosuite import load_part_controller_config
from termcolor import colored

from utils import (
    get_absolute_path_panda_xml,
    insert_line_after_mujoco_tag,
    replace_xml_tag_value,
    xml_modify_body_pos,
    xml_remove_subelement,
    xml_remove_tag_by_name,
    string_to_list,
    list_to_string,
)

import os

models_path = os.path.dirname(__file__) + "/franka_emika_panda"
robotname = "panda_bimanual"
default_scene_xml_path = models_path + "/scene_" + robotname + "_kitchen.xml"
default_robot_xml_path = models_path + "/" + robotname + ".xml"
default_robot_xml_temp_path = models_path + "/" + robotname + "_temp_abs.xml"

def get_styles() -> OrderedDict:
    raw_styles = dict(
        map(lambda item: (item.value, item.name.lower().capitalize()), StyleType)
    )
    styles = OrderedDict()
    for k in sorted(raw_styles.keys()):
        if k < 0:
            continue
        styles[k] = raw_styles[k]
    return styles


"""
Modified version of robocasa's kitchen scene generation script
https://github.com/robocasa/robocasa/blob/main/robocasa/demos/demo_kitchen_scenes.py
"""


def choose_option(options, option_name, show_keys=False, default=None, default_message=None):
    """
    Prints out environment options, and returns the selected env_name choice

    Returns:
        str: Chosen environment name
    """
    # get the list of all tasks

    if default is None:
        default = options[0]

    if default_message is None:
        default_message = default

    # Select environment to run
    print("{}s:".format(option_name.capitalize()))

    for i, (k, v) in enumerate(options.items()):
        if show_keys:
            print("[{}] {}: {}".format(i, k, v))
        else:
            print("[{}] {}".format(i, v))
    print()
    try:
        s = input(
            "Choose an option 0 to {}, or any other key for default ({}): ".format(
                len(options) - 1,
                default_message,
            )
        )
        # parse input into a number within range
        k = min(max(int(s), 0), len(options) - 1)
        choice = list(options.keys())[k]
    except Exception:
        if default is None:
            choice = options[0]
        else:
            choice = default
        print("Use {} by default.\n".format(choice))

    # Return the chosen environment name
    return choice


def model_generation_wizard(
    task: str = "PnPCounterToCab",
    layout: int = None,
    style: int = None,
    write_to_file: str = None,
    robot_spawn_pose: dict = None,
) -> Tuple[mujoco.MjModel, str, dict]:
    """
    Wizard/API to generate a kitchen model for a given task, layout, and style.
    If layout and style are not provided, it will take you through a wizard to choose them in the terminal.
    If robot_spawn_pose is not provided, it will spawn the robot to the default pose from robocasa fixtures.
    You can also write the generated xml model with absolutepaths to a file.
    The Object placements are made based on the robocasa defined Kitchen task and uses the default randomized
    placement distribution
    Args:
        task (str): task name
        layout (int): layout id
        style (int): style id
        write_to_file (str): write to file
        robot_spawn_pose (dict): robot spawn pose {pos: "x y z", quat: "x y z w"}
    Returns:
        Tuple[mujoco.MjModel, str, Dict]: model, xml string and Object placements info
    """
    layouts = OrderedDict(
        [
            (0, "One wall"),
            (1, "One wall w/ island"),
            (2, "L-shaped"),
            (3, "L-shaped w/ island"),
            (4, "Galley"),
            (5, "U-shaped"),
            (6, "U-shaped w/ island"),
            (7, "G-shaped"),
            (8, "G-shaped (large)"),
            (9, "Wraparound"),
        ]
    )

    styles = get_styles()
    if layout is None:
        layout = choose_option(
            layouts, "kitchen layout", default=-1, default_message="random layouts"
        )
    else:
        layout = layout

    if style is None:
        style = choose_option(styles, "kitchen style", default=-1, default_message="random styles")
    else:
        style = style

    if layout == -1:
        layout = np.random.choice(range(10))
        print(colored(f"Randomly choosing layout... id: {layout}", "yellow"))
    if style == -1:
        style = np.random.choice(range(11))
        print(colored(f"Randomly choosing style... id: {style}", "yellow"))

    # Create argument configuration
    # TODO: Figure how to get an env without robot arg
    config = {
        "env_name": task,
        "robots": "PandaMobile",
        "controller_configs": load_part_controller_config(default_controller="OSC_POSE"),
        "translucent_robot": False,
        "layout_and_style_ids": [[layout, style]],
    }

    print(colored("Initializing environment...", "yellow"))

    env = robosuite.make(
        **config,
        has_offscreen_renderer=False,
        render_camera=None,
        ignore_done=True,
        use_camera_obs=False,
        control_freq=20,
    )
    print(
        colored(
            f"Showing configuration:\n    Layout: {layout} {layouts[layout]}\n    Style: {style} {styles[style]}",
            "green",
        )
    )
    print()
    print(
        colored(
            "Spawning environment...\n",
            "yellow",
        )
    )
    model = env.sim.model._model
    xml = env.sim.model.get_xml()

    # Add the object placements to the xml
    click.secho(f"\nMaking Object Placements for task [{task}]...\n", fg="yellow")
    object_placements_info = {}
    for i in range(len(env.object_cfgs)):
        obj_name = env.object_cfgs[i]["name"]
        category = env.object_cfgs[i]["info"]["cat"]
        object_placements = env.object_placements
        print(
            f"Placing [Object {i}] (category: {category}, body_name: {obj_name}_main) at "
            f"pos: {np.round(object_placements[obj_name][0],2)} quat: {np.round(object_placements[obj_name][1],2)}"
        )
        xml = xml_modify_body_pos(
            xml,
            "body",
            obj_name + "_main",  # Object name ref in the xml
            pos=object_placements[obj_name][0],
            quat=object_placements[obj_name][1],
        )
        object_placements_info[obj_name + "_main"] = {
            "cat": category,
            "pos": object_placements[obj_name][0],
            "quat": object_placements[obj_name][1],
        }

    xml, robot_base_fixture_pose = custom_cleanups(xml)
    print(f"original pos:", robot_base_fixture_pose["pos"])
    print(f"original quat:", robot_base_fixture_pose["quat"])
    # robot_base_fixture_pose["pos"] = "3.2 -3.1 0.93"
    # robot_base_fixture_pose["quat"] = "0.7071068 0 0 0.7071068"
    # robot_base_fixture_pose["quat"] = "1 0 0 0"

    from config import layout_to_robot_spawn_qpos
    robot_base_fixture_pose = layout_to_robot_spawn_qpos[layout]
    
    if robot_spawn_pose is not None:
        robot_base_fixture_pose = robot_spawn_pose
        

    # robot_base_fixture_pose = {'name': 'robot0_base', 'pos': '0.5 -0.8 0', 'quat': '0.707107 0 0 0.707107'}   # in task 0, payout 0

    # add stretch to kitchen
    click.secho("\nMaking Robot Placement...\n", fg="yellow")
    xml = add_panda_to_kitchen(xml, robot_base_fixture_pose, default_robot_xml_temp_path)
    if write_to_file is not None:
        with open(write_to_file, "w") as f:
            f.write(xml)
        print(colored(f"Model saved to {write_to_file}", "green"))

    model = mujoco.MjModel.from_xml_string(xml)

    return model, xml, object_placements_info


def custom_cleanups(xml: str) -> Tuple[str, dict]:
    """
    Custom cleanups to models from robocasa envs to support
    use with stretch_mujoco package.
    """

    # make invisible the red/blue boxes around geom/sites of interests found
    xml = replace_xml_tag_value(xml, "geom", "rgba", "0.5 0 0 0.5", "0.5 0 0 0")
    xml = replace_xml_tag_value(xml, "geom", "rgba", "0.5 0 0 1", "0.5 0 0 0")
    xml = replace_xml_tag_value(xml, "site", "rgba", "0.5 0 0 1", "0.5 0 0 0")
    xml = replace_xml_tag_value(xml, "site", "actuator", "0.3 0.4 1 0.5", "0.3 0.4 1 0")
    # remove subelements
    xml = xml_remove_subelement(xml, "actuator")
    xml = xml_remove_subelement(xml, "sensor")

    # remove option tag element
    xml = xml_remove_subelement(xml, "option")
    # xml = xml_remove_subelement(xml, "size")

    # remove robot
    xml, remove_robot_attrib = xml_remove_tag_by_name(xml, "body", "robot0_base")

    return xml, remove_robot_attrib


def add_panda_to_kitchen(xml: str, 
                         robot_pose_attrib: dict,
                         robot_xml_temp_path: str) -> str:
    """
    Add panda robot to kitchen xml
    """
    print(
        f"Adding panda to kitchen at pos: {robot_pose_attrib['pos']} quat: {robot_pose_attrib['quat']}"
    )
    # panda_xml_absolute = get_absolute_path_panda_xml(robot_pose_attrib)
    panda_xml_absolute = get_absolute_path_panda_xml(robot_pose_attrib, default_robot_xml_path, robot_xml_temp_path)
    
    # add Panda xml
    xml = insert_line_after_mujoco_tag(
        xml,
        f' <include file="{panda_xml_absolute}"/>',
    )
    return xml


@click.command()
@click.option("--task", type=str, default="PnPCounterToCab", help="task")
@click.option("--layout", type=int, default=None, help="kitchen layout (choose number 0-9)")
@click.option("--style", type=int, default=None, help="kitchen style (choose number 0-11)")
@click.option("--write-to-file", type=str, default=None, help="write to file")
def main(task: str, layout: int, style: int, write_to_file: str):
    
    robot_spawn_pose = None
    # robot_spawn_pose = {
    #     "pos": "4.55 -0.8 0",
    #     "quat": "1 0 0 0",
    # }
    
    model, xml, objects_info = model_generation_wizard(
        task=task,
        layout=layout,
        style=style,
        write_to_file= default_scene_xml_path,
        robot_spawn_pose=robot_spawn_pose,
        
    )


if __name__ == "__main__":
    main()
