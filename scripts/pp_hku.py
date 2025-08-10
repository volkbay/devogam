import argparse
import multiprocessing
import os

import cv2
import numpy as np
import rosbag
import tqdm

from utils.bag_utils import (read_evs_from_rosbag, read_H_W_from_bag,
                             read_images_from_rosbag, read_poses_from_rosbag,
                             read_t0us_evs_from_rosbag,
                             read_tss_us_from_rosbag)
# from utils.event_utils import write_evs_arr_to_h5
# from utils.load_utils import compute_rmap_vector
# from utils.viz_utils import render

H, W = 260, 346


def render(x: np.ndarray, y: np.ndarray, pol: np.ndarray, H: int, W: int) -> np.ndarray:
    assert x.size == y.size == pol.size
    assert H > 0
    assert W > 0
    img = np.full((H,W,3), fill_value=255,dtype='uint8')
    mask = np.zeros((H,W),dtype='int32')
    pol = pol.astype('int')
    pol[pol==0]=-1
    mask1 = (x>=0)&(y>=0)&(W>x)&(H>y)
    mask[y[mask1].astype(np.int32), x[mask1].astype(np.int32)] = pol[mask1]
    img[mask==0]=[255,255,255]
    img[mask==-1]=[255,0,0]
    img[mask==1]=[0,0,255] 
    return img


def compute_rmap_vector(Kevsdist, dist_coeffs_evs, scenedir, side, H=480, W=640):
    K_new_evs, roi = cv2.getOptimalNewCameraMatrix(Kevsdist, dist_coeffs_evs, (W, H), alpha=0, newImgSize=(W, H))
    
    coords = np.stack(np.meshgrid(np.arange(W), np.arange(H))).reshape((2, -1)).astype("float32")
    term_criteria = (cv2.TERM_CRITERIA_MAX_ITER | cv2.TERM_CRITERIA_EPS, 100, 0.001)
    points = cv2.undistortPointsIter(coords, Kevsdist, dist_coeffs_evs, np.eye(3), K_new_evs, criteria=term_criteria)
    rectify_map = points.reshape((H, W, 2))

    # # 4) Create rectify map for events
    h5outfile = os.path.join(scenedir, f"rectify_map_{side}.h5")
    ef_out = h5py.File(h5outfile, 'w')
    ef_out.clear()
    ef_out.create_dataset('rectify_map', shape=(H, W, 2), dtype="<f4")
    ef_out["rectify_map"][:] = rectify_map
    ef_out.close()

    return rectify_map, K_new_evs


def compute_ms_to_idx(tss_ns, ms_start=0):
    """
    evs_ns: (N, 4)
    idx_start: Integer
    ms_start: Integer
    """

    ms_to_ns = 1000000
    # tss_sorted, _ = torch.sort(tss_ns) 
    # assert torch.abs(tss_sorted != tss_ns).sum() < 500

    ms_end = int(math.floor(tss_ns.max()) / ms_to_ns)
    assert ms_end >= ms_start
    ms_window = np.arange(ms_start, ms_end + 1, 1).astype(np.uint64)
    ms_to_idx = np.searchsorted(tss_ns, ms_window * ms_to_ns, side="left", sorter=np.argsort(tss_ns))
    
    assert np.all(np.asarray([(tss_ns[ms_to_idx[ms]] >= ms*ms_to_ns) for ms in ms_window]))
    assert np.all(np.asarray([(tss_ns[ms_to_idx[ms]-1] < ms*ms_to_ns) for ms in ms_window if ms_to_idx[ms] >= 1]))
    
    return ms_to_idx


def write_evs_arr_to_h5(evs, h5outfile, xidx=0, yidx=1, tssidx=2, polidx=3):
    ef_out = h5py.File(h5outfile, 'w')
    ef_out.clear()
    num_events = evs.shape[0]
    event_grp = ef_out.create_group('/events')
    event_grp.create_dataset('p', shape=(num_events,), dtype='|u1')
    event_grp.create_dataset('t', shape=(num_events,), dtype='<u4')
    event_grp.create_dataset('x', shape=(num_events,), dtype='<u2')
    event_grp.create_dataset('y', shape=(num_events,), dtype='<u2')
    event_grp["x"][:] = evs[:, xidx]
    event_grp["y"][:] = evs[:, yidx]
    event_grp["t"][:] = evs[:, tssidx]
    event_grp["p"][:] = evs[:, polidx]

    ms_to_idx = compute_ms_to_idx(evs[:, tssidx]*1e3)
    ef_out.create_dataset('ms_to_idx', shape=len(ms_to_idx), dtype="<u8")
    ef_out["ms_to_idx"][:] = ms_to_idx

    ef_out.close()


def write_gt_stamped(poses, tss_us_gt, outfile):
    with open(outfile, 'w') as f:
        for pose, ts in zip(poses, tss_us_gt):
            f.write(f"{ts} ")
            for i, p in enumerate(pose):
                if i < len(pose) - 1:
                    f.write(f"{p} ")
                else:
                    f.write(f"{p}")
            f.write("\n")


def get_calib_hku(side):  # hku uses davis
    if side == "left":
        intr = [249.69341447817564, 248.41625664694038,
                176.74240257052816, 129.47631010746218]
        distcoeffs = np.array([-0.3794794654640921, 0.15393049046270296,
                               0.0011400586965363895, -0.0019042695753031854])
        Kdist = np.eye(3)
        Kdist[0, 0] = intr[0]
        Kdist[1, 1] = intr[1]
        Kdist[0, 2] = intr[2]
        Kdist[1, 2] = intr[3]

    elif side == "right":
        intr = [258.61441518089174, 258.00363445501824,
                178.44356547141308, 135.84792628403616]
        distcoeffs = np.array([-0.3864639588089853, 0.1707517912637013,
                               -0.00046695742172563157, 0.0006610867041757214])

        Kdist = np.eye(3)
        Kdist[0, 0] = intr[0]
        Kdist[1, 1] = intr[1]
        Kdist[0, 2] = intr[2]
        Kdist[1, 2] = intr[3]

    return Kdist, distcoeffs


def process_seq_hku(indirs, side="left", DELTA_MS=None):
    for indir in indirs:
        seq = indir.split("/")[-1]
        print(f"\n\n HKU: Undistorting {seq} evs & rgb")

        inbag = os.path.join(indir, f"../{seq}.bag")
        bag = rosbag.Bag(inbag, "r")
        topics = list(bag.get_type_and_topic_info()[1].keys())
        if side == "left":
            imgtopic_idx = 2
            evtopic_idx = 1
        elif side == "right":
            imgtopic_idx = 5
            evtopic_idx = 4
        else:
            raise NotImplementedError

        imgdirout = os.path.join(indir, f"images_undistorted_{side}")
        Hbag, Wbag = read_H_W_from_bag(bag, topics[imgtopic_idx])
        assert (Hbag == H and Wbag == W)

        if not os.path.exists(imgdirout):
            os.makedirs(imgdirout)
        else:
            img_list_undist = [os.path.join(indir, imgdirout, im)
                               for im in sorted(os.listdir(imgdirout))
                               if im.endswith(".png")]
            undist_no = bag.get_message_count(topics[imgtopic_idx])
            if undist_no == len(img_list_undist):
                print("\n\nWARNING **** Images already undistorted.",
                      f" Skipping {indir} ***** \n\n")
                assert os.path.isfile(
                    os.path.join(indir, f"rectify_map_{side}.h5"))
                continue

        imgs = read_images_from_rosbag(bag, topics[imgtopic_idx], H=H, W=W)
        imgs = [cv2.resize(img, (W, H)) for img in imgs]
        Kdist, distcoeffs = get_calib_hku(side)

        # undistorting images
        K_new, roi = cv2.getOptimalNewCameraMatrix(Kdist, distcoeffs, (W, H),
                                                   alpha=0, newImgSize=(W, H))
        f = open(os.path.join(indir, f"calib_undist_{side}.txt"), 'w')
        f.write(f"{K_new[0, 0]} {K_new[1, 1]} {K_new[0, 2]} {K_new[1, 2]}")
        f.close()

        img_mapx, img_mapy = cv2.initUndistortRectifyMap(
            Kdist, distcoeffs, np.eye(3), K_new, (W, H), cv2.CV_32FC1)
        # undistorting images
        pbar = tqdm.tqdm(total=len(imgs)-1)
        for i, img in enumerate(imgs):
            # DEBUG only
            # cv2.imwrite(os.path.join(imgdirout, f"{i:012d}_DIST.png"), img)
            img = cv2.remap(img, img_mapx, img_mapy, cv2.INTER_CUBIC)
            cv2.imwrite(os.path.join(imgdirout, f"{i:012d}.png"), img)
            pbar.update(1)
        imgs = []

        # writing pose to file
        posetopic = '/cpy_uav/viconros/odometry'
        if side == "left":
            T_cam0_cam1 = np.eye(4)
        else:
            T_cam0_cam1 = np.array([
                [0.9999189999842378, 0.00927392731970859,
                 -0.00871709484799569, -0.05968052204060377],
                [-0.009231577824269699, 0.9999454511978819,
                 0.004885959428529005, -0.0005334476469976882],
                [0.008761931373541011, -0.004805091126247473,
                 0.9999500685823629, 0.0005990728587972945],
                [0.0, 0.0, 0.0, 1.0]])

        # TODO: check inv or not?
        T_marker_cam0 = np.linalg.inv(
                        np.array([
                            [0.9999552277012158, -0.00603191153357543,
                             0.007290996931816412, 0.00011018857347815285],
                            [0.005994670026470383, 0.9999689294906282,
                             0.005118982773930891, -0.0007730487905611042],
                            [-0.007321647648062164, -0.005075046464534421,
                             0.9999603179022153, -0.060160984076249716],
                            [0.0, 0.0, 0.0, 1.0]]))

        tss_imgs_us = read_tss_us_from_rosbag(bag, topics[imgtopic_idx])
        # assert len(tss_imgs_us) == len(imgs)
        poses, tss_gt_us = read_poses_from_rosbag(
            bag, posetopic, T_marker_cam0, T_cam0_cam1=T_cam0_cam1)
        t0_evs = read_t0us_evs_from_rosbag(bag, topics[evtopic_idx])
        assert sorted(tss_imgs_us) == tss_imgs_us
        assert sorted(tss_gt_us) == tss_gt_us

        t0_us = np.minimum(np.minimum(tss_gt_us[0], tss_imgs_us[0]), t0_evs)
        tss_imgs_us = [t - t0_us for t in tss_imgs_us]

        # saving tss
        f = open(os.path.join(indir, f"tss_imgs_us_{side}.txt"), 'w')
        for t in tss_imgs_us:
            f.write(f"{t:.012f}\n")
        f.close()

        tss_gt_us = [t - t0_us for t in tss_gt_us]
        write_gt_stamped(poses, tss_gt_us, os.path.join(
            indir, f"gt_stamped_{side}.txt"))

        # write events (and also substract t0_evs)
        evs = read_evs_from_rosbag(bag, topics[evtopic_idx], H=H, W=W)
        for ev in evs:
            ev[2] -= t0_us
        h5outfile = os.path.join(indir, f"evs_{side}.h5")
        write_evs_arr_to_h5(evs, h5outfile)

        rectify_map, K_new_evs = compute_rmap_vector(
            Kdist, distcoeffs, indir, side, H=H, W=W)
        assert np.all(abs(K_new_evs - K_new) < 1e-5)

        # [DEBUG] viz undistorted events
        outvizfolder = os.path.join(indir, f"evs_{side}_undist")
        os.makedirs(outvizfolder, exist_ok=True)
        pbar = tqdm.tqdm(total=len(tss_imgs_us)-1)
        for (ts_idx, ts_us) in enumerate(tss_imgs_us):
            if ts_idx == len(tss_imgs_us) - 1:
                break

            if DELTA_MS is None:
                evs_idx = np.where(
                    (evs[:, 2] >= ts_us) & (evs[:, 2] < tss_imgs_us[ts_idx+1])
                    )[0]
            else:
                evs_idx = np.where(
                    (evs[:, 2] >= ts_us) & (evs[:, 2] < ts_us + DELTA_MS*1e3)
                    )[0]

            if len(evs_idx) == 0:
                print(f"no events in range {ts_us*1e-3}",
                      f" - {tss_imgs_us[ts_idx+1]*1e-3} milisecs")
                continue
            evs_batch = np.array(evs[evs_idx, :]).copy()

            img = render(evs_batch[:, 0], evs_batch[:, 1],
                         evs_batch[:, 3], H, W)
            imfnmae = os.path.join(outvizfolder, f"{ts_idx:06d}_dist.png")
            cv2.imwrite(imfnmae, img)

            rect = rectify_map[evs_batch[:, 1].astype(np.int32),
                               evs_batch[:, 0].astype(np.int32)]
            img = render(rect[:, 0], rect[:, 1], evs_batch[:, 3], H, W)

            imfnmae = imfnmae.split(".")[0] + ".png"
            cv2.imwrite(os.path.join(outvizfolder, imfnmae), img)

            pbar.update(1)
        # [end DEBUG] viz undistorted events

        print(f"Finshied processing {indir}\n\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PP HKU data in dir")
    parser.add_argument(
        "--indir", help="Input image directory.", default=""
    )
    args = parser.parse_args()

    roots = []
    for root, dirs, files in os.walk(args.indir):
        for f in files:
            if f.endswith(".bag"):
                p = os.path.join(root, f"{f.split('.')[0]}")
                os.makedirs(p, exist_ok=True)
                if p not in roots:
                    roots.append(p)

    cors = 4
    assert cors <= 9
    roots_split = np.array_split(roots, cors)

    processes = []
    for i in range(cors):
        p = multiprocessing.Process(target=process_seq_hku,
                                    args=(roots_split[i].tolist(),))
        p.start()
        processes.append(p)

    for p in processes:
        p.join()

    print("Finished processing all HKU scenes")
