import time

import numpy as np

from dobot_control.utils import wait_period


def set_light(env, which_color, which_status):
    print("light change")
    if which_color == "red":
        env.set_do_status([3, 0])
        env.set_do_status([2, 0])
        env.set_do_status([1, which_status])
    elif which_color == "yellow":
        env.set_do_status([3, 0])
        env.set_do_status([2, which_status])
        env.set_do_status([1, 0])
    elif which_color == "green":
        env.set_do_status([3, which_status])
        env.set_do_status([2, 0])
        env.set_do_status([1, 0])
    return which_color

# robot init move
def robot_pose_init(env):
    # go to the first point
    reset_joints_left = np.deg2rad([-90, 30, -110, 20, 90, 90, 0])  #
    reset_joints_right = np.deg2rad([90, -30, 110, -20, -90, -90, 0])
    reset_joints = np.concatenate([reset_joints_left, reset_joints_right])
    curr_joints = env.get_obs()["joint_positions"]
    max_delta = (np.abs(curr_joints - reset_joints)).max()
    steps = min(int(max_delta / 0.01), 100)
    for jnt in np.linspace(curr_joints, reset_joints, steps):
        env.step(jnt, [1, 1])

    # go to the second point
    reset_joints_left = np.deg2rad([-90, 0, -90, 0, 90, 90, 0])  #
    reset_joints_right = np.deg2rad([90, 0, 90, 0, -90, -90, 0])
    reset_joints = np.concatenate([reset_joints_left, reset_joints_right])
    curr_joints = env.get_obs()["joint_positions"]
    max_delta = (np.abs(curr_joints - reset_joints)).max()
    steps = min(int(max_delta / 0.01), 100)
    for jnt in np.linspace(curr_joints, reset_joints, steps):
        env.step(jnt, [1, 1])


# nova2 dev joint check
def servo_action_check(action, last_action, flag_in, step_len=0.9):
    ind_list = [i for i in range(14)]
    assert len(ind_list), "err in servo_action_check"
    if (np.abs(action - last_action) > step_len).any():
        joint_index = np.where(np.abs(action - last_action) > step_len)
        print("action: ", action)
        print("last_action: ", last_action)
        for j in joint_index[0]:
            if j != 6 and j != 13 and (j in ind_list):
                pi_2_cal = (action[j] - last_action[j])/np.pi
                if abs(pi_2_cal) > 1.85 and abs(pi_2_cal) < 2.15:
                    action[j] = action[j] - 2 * np.pi * (pi_2_cal / abs(pi_2_cal))
                else:
                    print("Servo action dev is too big")
                    print(
                        f"Joint [{j}], leader: {action[j]}, follower: {last_action[j]}, "
                        f"diff: {(action[j] - last_action[j])/np.pi}")
                    return 0, action
    return 1, action


# pose check between main hand and the follower
def pose_check(env, agent, flag_in):
    start_pos = agent.act(env.get_obs())
    obs = env.get_obs()
    joints = obs["joint_positions"]
    err_pose_check, action_return = servo_action_check(start_pos, joints, flag_in, 0.6)
    if err_pose_check:
        return 1, action_return
    else:
        return 0, action_return


def dynamic_approach(env, agent, flag_in):
    err1, action1 = pose_check(env, agent, flag_in)
    assert err1 != 0, set_light(env, "red", 1)
    obs = env.get_obs()
    joints = obs["joint_positions"]
    # log_write(__file__, "joints: " + str(joints))
    # log_write(__file__, "action1: " + str(action1))
    joints[6] = action1[6]
    joints[13] = action1[13]
    if flag_in[0] and not flag_in[1]:
        abs_deltas = max(np.abs(action1[:6] - joints[:6]))
    elif not flag_in[0] and flag_in[1]:
        abs_deltas = max(np.abs(action1[7:13] - joints[7:13]))
    else:
        abs_deltas = max(np.abs(action1 - joints))
    # log_write(__file__,  "abs_deltas: " + str(abs_deltas))
    steps = int(abs_deltas / 0.01)
    # log_write(__file__, "steps: " + str(steps))

    for jnt in np.linspace(joints, action1, steps):
        env.step(jnt, flag_in)
        tic = time.time()
        wait_period(50, tic)

        # log_write(__file__, "flag_in: " + str(flag_in))
        # log_write(__file__, "jnt: " + str(jnt))
    # time.sleep(0.05)
    err1, action1 = pose_check(env, agent, flag_in)
    assert err1 != 0, set_light(env, "red", 1)
    return action1
