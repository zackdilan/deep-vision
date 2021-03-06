import argparse
import time

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
from torchsummary import summary
from torchvision import transforms

from data_load import (CenterCrop, ImageNet2012Dataset, Normalize, RandomCrop,
                       RandomHorizontalFlip, Rescale, ToTensor, ColorJitter)
from models.alexnet_v1 import AlexNetV1
from models.alexnet_v2 import AlexNetV2
from models.inception_v1 import InceptionV1
from models.resnet34 import ResNet34
from models.resnet50 import ResNet50
from models.vgg16 import VGG16
from models.vgg19 import VGG19
from models.mobilenet_v1 import MobileNetV1

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
model_dir = './saved_models/'
desired_image_shape = torch.empty(3, 224, 224).size()

training_config = {
    'alexnet1': {
        'name': 'alexnet1',
        # "We trained our models using stochastic gradient descent with a batch size
        # of 128 examples" alexnet1.[1]
        'batch_size': 128,
        'num_workers': 1,
        'model': AlexNetV1,
        'optimizer': optim.SGD,
        # "...momentum of 0.9, and weight decay of 0.0005...The learning rate was
        # initialized at 0.01..." alexnet1.[1]
        'optimizer_params': {
            'lr': 0.01,
            'momentum': 0.9,
            'weight_decay': 0.0005,
        },
        # "The heuristic which we followed was to divide the learning rate by 10
        # when the validation error rate stopped improving withthe current
        # learning rate." alextnet1.[1]
        'scheduler': optim.lr_scheduler.ReduceLROnPlateau,
        'scheduler_params': {
            'factor': 0.1,
            'mode': 'max',
        },
        'total_epochs': 200,
    },
    'alexnet2': {
        'name': 'alexnet2',
        'batch_size': 128,
        'num_workers': 16,
        'model': AlexNetV2,
        'optimizer': optim.SGD,
        # "I trained all models for exactly 90 epochs, and multiplied the learning rate
        # by 250^(1/3) at 25%, 50%,and 75% training progress" alexnet2.[1]
        # "...momentum may be less necessary...
        # but in my experiments I used mu = 0.9..." alexnet2.[1]
        # I used the same lr policy as alexnet1 above though
        'optimizer_params': {
            'lr': 0.01,
            'momentum': 0.9,
            'weight_decay': 0.0005,
        },
        'scheduler': optim.lr_scheduler.ReduceLROnPlateau,
        'scheduler_params': {
            'factor': 0.1,
            'mode': 'max',
        },
        'total_epochs': 200,
    }
}


def initialize_train_loader(transform, config):
    train_dataset = ImageNet2012Dataset(
        root_dir='../dataset/train_flatten/',
        labels_file='../dataset/synsets.txt',
        transform=transform,
    )
    print('Number of train images: ', len(train_dataset))

    assert train_dataset[0]['image'].size(
    ) == desired_image_shape, "Wrong train image dimension!"

    train_loader = DataLoader(
        train_dataset,
        batch_size=config.get('batch_size'),
        shuffle=True,
        num_workers=config.get('num_workers'),
    )

    return train_loader


def initialize_val_loader(transform, config):
    val_dataset = ImageNet2012Dataset(
        root_dir='../dataset/val_flatten/',
        labels_file='../dataset/synsets.txt',
        transform=transform,
    )
    print('Number of validation images: ', len(val_dataset))

    assert val_dataset[0]['image'].size(
    ) == desired_image_shape, "Wrong validation image dimension"

    val_loader = DataLoader(
        val_dataset,
        batch_size=config.get('batch_size'),
        shuffle=False,
        num_workers=config.get('num_workers'),
    )

    return val_loader


def initialize_loggers():
    loggers = {
        'train_loss': {
            'epochs': [],
            'value': [],
        },
        'val_loss': {
            'epochs': [],
            'value': [],
        },
        'val_top1_acc': {
            'epochs': [],
            'value': [],
        },
        'val_top5_acc': {
            'epochs': [],
            'value': [],
        }
    }
    return loggers


def log_metrics(loggers, name, value, epoch):
    logger = loggers.get(name)
    logger.get('epochs').append(epoch)
    logger.get('value').append(value)


def get_lr(optimizer):
    for param_group in optimizer.param_groups:
        return param_group['lr']


def load_checkpoint(checkpoint_path, net, optimizer, scheduler, loggers):
    checkpoint = torch.load(checkpoint_path)
    net.load_state_dict(checkpoint['model'])
    optimizer.load_state_dict(checkpoint['optimizer'])
    # https://github.com/pytorch/pytorch/issues/2830#issuecomment-336194949
    if torch.cuda.is_available():
        for state in optimizer.state.values():
            for k, v in state.items():
                if isinstance(v, torch.Tensor):
                    state[k] = v.cuda()
    scheduler.load_state_dict(checkpoint['scheduler'])
    start_epoch = checkpoint['epoch'] + 1
    loggers = checkpoint['loggers']

    return net, optimizer, scheduler, loggers, start_epoch


def run_epochs(config, checkpoint_path):
    print("CUDA is available: {}".format(torch.cuda.is_available()))

    # Define data loader: data preprocessing and augmentation
    # I use same procedures for all models that consumes imagenet-2012 dataset for simplicity
    imagenet_train_transform = transforms.Compose([
        Rescale(256),
        RandomHorizontalFlip(0.5),
        RandomCrop(224),
        ColorJitter(brightness=0.2, contrast=0.2, saturation=0.2, hue=0),
        ToTensor(),
        # https://github.com/pytorch/examples/blob/master/imagenet/main.py#L195
        # this is pre-calculated mean and std of imagenet dataset
        Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])

    imagenet_val_transform = transforms.Compose([
        Rescale(256),
        CenterCrop(224),
        ToTensor(),
        Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])

    train_loader = initialize_train_loader(imagenet_train_transform, config)
    val_loader = initialize_val_loader(imagenet_val_transform, config)

    # Define the neural network.
    Model = config.get('model')
    model_params = config.get('model_params')
    if model_params is not None:
        net = Model(**model_params)
    else:
        net = Model()

    # transfer variables to GPU if present
    net.to(device=device)

    # Print the network structure given 3x32x32 input
    # need to put this before DataParallel to avoid "Expected more than 1 value per channel when training" error
    # https://github.com/pytorch/pytorch/issues/4534
    summary(net, (3, 224, 224))

    # Wrap it with DataParallel to train with multiple GPUs
    if torch.cuda.device_count() > 1:
        print("Using", torch.cuda.device_count(), "GPUs!")
        net = nn.DataParallel(net)

    # Define the loss function. CrossEntrophyLoss is the most common one for classification task.
    criterion = nn.CrossEntropyLoss()

    # Define the optimizer
    Optim = config.get('optimizer')
    optimizer = Optim(
        net.parameters(),
        **config.get('optimizer_params'),
    )

    # Define the scheduler
    Sched = config.get('scheduler')
    scheduler = Sched(
        optimizer,
        **config.get('scheduler_params'),
    )

    loggers = initialize_loggers()

    model_id = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
    model_name = config.get('name')

    start_epoch = 1

    if checkpoint_path is not None:
        net, optimizer, scheduler, loggers, start_epoch = load_checkpoint(
            checkpoint_path,
            net,
            optimizer,
            scheduler,
            loggers,
        )

    validate(val_loader, net, criterion, 0, loggers)

    for epoch in range(start_epoch, config.get('total_epochs') + 1):

        train(
            train_loader,
            net,
            criterion,
            optimizer,
            epoch,
            loggers,
        )

        val_loss, top1_acc, top5_acc = validate(
            val_loader,
            net,
            criterion,
            epoch,
            loggers,
        )

        # for ReduceLROnPlateau scheduler, we need to use top1_acc as metric
        if isinstance(scheduler, optim.lr_scheduler.ReduceLROnPlateau):
            scheduler.step(top1_acc)
        else:
            scheduler.step()

        checkpoint_file = '{}-{}-epoch-{}.pt'.format(
            model_name,
            model_id,
            epoch,
        )
        torch.save({
            'epoch': epoch,
            'model': net.state_dict(),
            'optimizer': optimizer.state_dict(),
            'scheduler': scheduler.state_dict(),
            'loggers': loggers,
        }, model_dir + checkpoint_file)


def train(train_loader, net, criterion, optimizer, epoch, loggers):
    # mark as train mode
    net.train()
    # initialize the batch_loss to help us understand the performance of multiple batches
    batches_loss = 0.0
    print("Start training epoch {}".format(epoch))

    for batch_i, data in enumerate(train_loader):
        # extract images and annotations
        image = data.get('image')
        annotation = data.get('annotation')

        # annotation is an integer index
        annotation = annotation.to(device=device, dtype=torch.long)
        # PyTorch likes float type for image. So we convert to it.
        image = image.to(device=device, dtype=torch.float)

        # forward propagation - calculate the output
        output = net(image)

        # calculate the loss
        loss = criterion(output, annotation)

        # https://discuss.pytorch.org/t/why-do-we-need-to-set-the-gradients-manually-to-zero-in-pytorch/4903/8
        # https://stackoverflow.com/questions/44732217/why-do-we-need-to-explicitly-call-zero-grad
        # zero the parameter (weight) gradients
        optimizer.zero_grad()

        # back propogate and calculate differentiation
        loss.backward()

        # get current learning rate
        lr = get_lr(optimizer)

        # https://discuss.pytorch.org/t/how-are-optimizer-step-and-loss-backward-related/7350
        # update weights by stepping optimizer
        optimizer.step()

        # accumulate the running loss
        batches_loss += loss.item()

        if batch_i % 10 == 9:  # print every 10 batches
            batches_loss = batches_loss / 10.0
            print('Time, {}, Epoch: {}, Batch: {}, Training Loss: {}, LR: {}'.
                  format(
                      time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                      epoch,
                      batch_i + 1,  # batch_i start from 0
                      batches_loss,
                      lr,
                  ))

            log_metrics(loggers, 'train_loss', batches_loss, epoch)

            batches_loss = 0.0


def validate(val_loader, net, criterion, epoch, loggers):
    net.eval()
    total_loss = 0
    top1_acc = 0.0
    top5_acc = 0.0
    # turn off grad to avoid cuda out of memory error
    with torch.no_grad():
        for batch_i, data in enumerate(val_loader):
            image = data.get('image')
            annotation = data.get('annotation')

            annotation = annotation.to(device=device, dtype=torch.long)
            image = image.to(device=device, dtype=torch.float)

            output = net(image)
            loss = criterion(output, annotation)
            acc1, acc5 = accuracy(output, annotation, topk=(1, 5))
            top1_acc += acc1[0]
            top5_acc += acc5[0]
            total_loss += loss

    top1_acc = top1_acc / len(val_loader)
    top5_acc = top5_acc / len(val_loader)
    print('Epoch: {}, Validation Top 1 acc: {}'.format(epoch, top1_acc))
    print('Epoch: {}, Validation Top 5 acc: {}'.format(epoch, top5_acc))
    val_loss = total_loss / len(val_loader)
    print('Epoch: {}, Validation Set Loss: {}'.format(epoch, val_loss))

    log_metrics(loggers, 'val_top1_acc', top1_acc, epoch)
    log_metrics(loggers, 'val_top5_acc', top5_acc, epoch)
    log_metrics(loggers, 'val_loss', val_loss, epoch)

    return val_loss, top1_acc, top5_acc


# https://github.com/pytorch/examples/blob/master/imagenet/main.py#L381
def accuracy(output, target, topk=(1, )):
    """Computes the accuracy over the k top predictions for the specified values of k"""
    with torch.no_grad():
        maxk = max(topk)
        batch_size = target.size(0)

        _, pred = output.topk(maxk, 1, True, True)
        pred = pred.t()
        correct = pred.eq(target.view(1, -1).expand_as(pred))

        res = []
        for k in topk:
            correct_k = correct[:k].view(-1).float().sum(0, keepdim=True)
            res.append(correct_k.mul_(100.0 / batch_size))
        return res


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    supported_models = list(training_config.keys())
    parser.add_argument(
        "-m",
        "--model",
        type=str,
        required=True,
        choices=supported_models,
        help="specify model name",
    )
    parser.add_argument(
        "-c",
        "--checkpoint",
        type=str,
        help="specify checkpoint file path",
    )
    args = parser.parse_args()
    model_name = args.model
    checkpoint_path = args.checkpoint
    config = training_config.get(model_name)
    run_epochs(config, checkpoint_path)
