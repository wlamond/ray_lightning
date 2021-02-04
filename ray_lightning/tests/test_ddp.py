# test correct gpu devices set
# test model moved to gpu correctly
# test multi-nodes gpu
import os

import pytest
import ray
import pytorch_lightning as pl
from pl_bolts.datamodules import MNISTDataModule
from pytorch_lightning import Callback
from pytorch_lightning.callbacks import EarlyStopping
from ray.tune.examples.mnist_ptl_mini import LightningMNISTClassifier
from torch.utils.data import DistributedSampler

from ray_lightning import RayAccelerator
from ray_lightning.tests.utils import get_trainer, train_test, \
    load_test, predict_test, BoringModel


@pytest.fixture
def ray_start_2_cpus():
    address_info = ray.init(num_cpus=2)
    yield address_info
    ray.shutdown()

@pytest.mark.parametrize("num_workers", [1, 2])
def test_actor_creation(tmpdir, ray_start_2_cpus, num_workers):
    """Tests whether the appropriate number of training actors are created."""
    model = BoringModel()
    def check_num_actor():
        assert len(ray.actors()) == num_workers
    model.on_epoch_end = check_num_actor
    accelerator = RayAccelerator(num_workers=num_workers)
    trainer = get_trainer(tmpdir, accelerator=accelerator)
    trainer.fit(model)
    assert all(actor["State"] == ray.gcs_utils.ActorTableData.DEAD
               for actor in list(ray.actors().values()))

def test_distributed_sampler(tmpdir, ray_start_2_cpus):
    """Makes sure the distributed sampler is properly set."""
    model = BoringModel()
    train_dataloader = model.train_dataloader()
    initial_sampler = train_dataloader.sampler
    assert not isinstance(initial_sampler, DistributedSampler)

    class DistributedSamplerCallback(Callback):
        def on_train_start(self, trainer, pl_module):
            train_sampler = trainer.train_dataloader.sampler
            assert isinstance(train_sampler, DistributedSampler)
            assert train_sampler.shuffle
            assert train_sampler.num_replicas == 2
            assert train_sampler.rank == trainer.global_rank

        def on_validation_start(self, trainer, pl_module):
            train_sampler = trainer.val_dataloaders[0].sampler
            assert isinstance(train_sampler, DistributedSampler)
            assert not train_sampler.shuffle
            assert train_sampler.num_replicas == 2
            assert train_sampler.rank == trainer.global_rank

        def on_test_start(self, trainer, pl_module):
            train_sampler = trainer.test_dataloaders[0].sampler
            assert isinstance(train_sampler, DistributedSampler)
            assert not train_sampler.shuffle
            assert train_sampler.num_replicas == 2
            assert train_sampler.rank == trainer.global_rank

    accelerator = RayAccelerator(num_workers=2)
    trainer = get_trainer(tmpdir, accelerator=accelerator,
                          callbacks=[DistributedSamplerCallback()])
    trainer.fit(model)


@pytest.mark.parametrize("num_workers", [1, 2])
def test_train(tmpdir, ray_start_2_cpus, num_workers):
    model = BoringModel()
    accelerator = RayAccelerator(num_workers=num_workers)
    trainer = get_trainer(tmpdir, accelerator=accelerator)
    train_test(trainer, model)

@pytest.mark.parametrize("num_workers", [1, 2])
def test_load(tmpdir, ray_start_2_cpus, num_workers):
    model = BoringModel()
    accelerator = RayAccelerator(num_workers=num_workers, use_gpu=False)
    trainer = get_trainer(tmpdir, accelerator=accelerator)
    load_test(trainer, model)

@pytest.mark.parametrize("num_workers", [1, 2])
def test_predict(tmpdir, ray_start_2_cpus, seed, num_workers):
    config = {
        "layer_1": 32,
        "layer_2": 32,
        "lr": 1e-2,
        "batch_size": 32,
    }
    model = LightningMNISTClassifier(config, tmpdir)
    dm = MNISTDataModule(
        data_dir=tmpdir, num_workers=1, batch_size=config["batch_size"])
    accelerator = RayAccelerator(num_workers=num_workers, use_gpu=False)
    trainer = get_trainer(
        tmpdir, limit_train_batches=10, max_epochs=1, accelerator=accelerator)
    predict_test(trainer, model, dm)

def test_early_stop(tmpdir, ray_start_2_cpus):
    model = BoringModel()
    accelerator = RayAccelerator(num_workers=1, use_gpu=False)
    early_stop = EarlyStopping(monitor="val_loss", patience=2, verbose=True)
    trainer = get_trainer(tmpdir, max_epochs=500, accelerator=accelerator,
                          callbacks=[early_stop], limit_train_batches=1.0,
                          limit_val_batches=1.0, progress_bar_refresh_rate=1)
    trainer.fit(model)
    trained_model = BoringModel.load_from_checkpoint(
        trainer.checkpoint_callback.best_model_path)
    assert trained_model.val_epoch == 2, trained_model.val_epoch