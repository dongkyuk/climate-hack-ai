import os
import hydra
from typing import Optional
from dataclasses import dataclass, field
from hydra.core.config_store import ConfigStore


@dataclass
class NeptuneConfig:
    use_neptune: Optional[bool] = None
    project_name: str = "dongkyuk/climate-hack-ai"
    exp_name: Optional[str] = "conv gru"
    tags : Optional[tuple]= ("unet", "cnn")


@dataclass
class TrainerConfig:
    gpus: tuple = (0,1)
    num_workers: int = 4 * len(gpus)
    seed: int = 42
    pin_memory: bool = True
    persistent_workers: bool = True
    val_check_interval: float = 1000

    epoch: int = 1000
    lr: float = 1e-5
    train_batch_size: int = 8 #16
    val_batch_size: int = 8 #16


@dataclass
class PathConfig:
    data_dir: str = "/data/climate_hack"
    xarr_dataset_path: str = "/home/dongkyun/Desktop/Other/climate-hack-ai/data/satellite/EUMETSAT/SEVIRI_RSS/v3/eumetsat_seviri_hrv_uk.zarr"
    save_dir: Optional[str] = "save"

@dataclass
class Config:
    neptune_cfg: NeptuneConfig = NeptuneConfig()
    trainer_cfg: TrainerConfig = TrainerConfig()
    path_cfg: PathConfig = PathConfig()
    path_cfg.save_dir = os.path.join(path_cfg.save_dir, neptune_cfg.exp_name)
    os.makedirs(path_cfg.save_dir, exist_ok=True)


def register_configs() -> None:
    cs = ConfigStore.instance()
    cs.store(name="config", node=Config)