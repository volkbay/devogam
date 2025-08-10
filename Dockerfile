FROM nvidia/cuda:11.3.1-cudnn8-devel-ubuntu20.04

RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get \
    install -y --no-install-recommends \
    nano wget curl git build-essential ca-certificates \
    fish libglib2.0-0 libsm6 libxext6 libxrender1 \
    unzip libgl1-mesa-glx\
    && rm -rf /var/lib/apt/lists/*

ENV CONDA_DIR=/opt/conda
ENV PATH=$CONDA_DIR/bin:$PATH

# Install Miniconda
RUN wget --quiet \
    https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh \
    -O /tmp/miniconda.sh && \
    bash /tmp/miniconda.sh -b -p $CONDA_DIR && \
    rm /tmp/miniconda.sh && \
    $CONDA_DIR/bin/conda config --set always_yes yes --set changeps1 no && \
    $CONDA_DIR/bin/conda tos accept --override-channels \
    --channel https://repo.anaconda.com/pkgs/main && \
    $CONDA_DIR/bin/conda tos accept --override-channels \
    --channel https://repo.anaconda.com/pkgs/r && \
    $CONDA_DIR/bin/conda update -q conda

# Install mamba
RUN conda install mamba -n base -c conda-forge

# Copy environment file
COPY environment.yml /tmp/environment.yml

# Create environment
RUN mamba env create -f /tmp/environment.yml && conda clean -afy
ENV PATH=$CONDA_DIR/envs/devo/bin:$PATH

WORKDIR /devogam
RUN conda init fish
RUN conda activate devo && \
    pip install .

# Default command shell
RUN echo "export QT_X11_NO_MITSHM=1" >> /root/.bashrc
RUN echo "export NVIDIA_VISIBLE_DEVICES=all" >> /root/.bashrc
RUN echo "export NVIDIA_DRIVER_CAPABILITIES=all" \
    >> /root/.bashrc
RUN echo 'set -x QT_X11_NO_MITSHM 1' >> /root/.config/fish/config.fish
RUN echo 'set -x NVIDIA_VISIBLE_DEVICES all' >> /root/.config/fish/config.fish
RUN echo 'set -x NVIDIA_DRIVER_CAPABILITIES all' \
    >> /root/.config/fish/config.fish
CMD ["/bin/fish"]

# Running Container:
# export DATASET_LOC=/home/.../set/devo # Host dataset location
# export PRJ_LOC=/home/.../prj/devogam # Host project location
# docker run -it --privileged --net=host \
#     -v /dev/bus/usb:/dev/bus/usb \
#     -v /run/user/1000/gdm/Xauthority:/root/.Xauthority \
#     -v /run/user/1000/:/run/user/1000/ \
#     -v /tmp/.X11-unix/:/tmp/.X11-unix/ \
#     -v $DATASET_LOC:/data \
#     -v $PRJ_LOC:/devogam \
#     -e DISPLAY --env=NVIDIA_DRIVER_CAPABILITIES=all \
#     --gpus all --runtime=nvidia devo