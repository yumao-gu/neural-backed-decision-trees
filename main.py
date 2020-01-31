'''Train CIFAR10 with PyTorch.'''
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import torch.backends.cudnn as cudnn
from utils.datasets import CIFAR10NodeDataset, CIFAR10PathSanityDataset

import torchvision
import torchvision.transforms as transforms

import os
import argparse

import models
from utils.utils import progress_bar, initialize_confusion_matrix, \
    update_confusion_matrix, confusion_matrix_recall, confusion_matrix_precision, \
    set_np_printoptions, generate_fname, CIFAR10NODE, CIFAR10PATHSANITY


datasets = ('CIFAR10', 'CIFAR100', CIFAR10NODE, CIFAR10PATHSANITY)


parser = argparse.ArgumentParser(description='PyTorch CIFAR Training')
parser.add_argument('--batch-size', default=128, type=int,
                    help='Batch size used for training')
parser.add_argument('--epochs', '-e', default=350, type=int,
                    help='By default, lr schedule is scaled accordingly')
parser.add_argument('--dataset', default='CIFAR10', choices=datasets)
parser.add_argument('--model', default='ResNet18', choices=list(models.get_model_choices()))
parser.add_argument('--lr', default=0.1, type=float, help='learning rate')
parser.add_argument('--resume', '-r', action='store_true', help='resume from checkpoint')

parser.add_argument('--wnid', help='wordnet id for cifar10node dataset',
                    default='fall11')
parser.add_argument('--eval', help='eval only', action='store_true')
parser.add_argument('--test', action='store_true', help='run dataset tests')
parser.add_argument('--test-path-sanity', action='store_true',
                    help='test path classifier with oracle fc')
parser.add_argument('--test-path', action='store_true',
                    help='test path classifier with random init')
parser.add_argument('--print-confusion-matrix', action='store_true')

args = parser.parse_args()


if args.test:
    import xml.etree.ElementTree as ET

    dataset = CIFAR10PathSanityDataset()
    print(dataset[0][0])

    for wnid, text in (
            ('fall11', 'root'),
            ('n03575240', 'instrument'),
            ('n03791235', 'motor vehicle'),
            ('n02370806', 'hoofed mammal')):
        dataset = CIFAR10NodeDataset(wnid)

        print(text)
        print(dataset.node.mapping)

    with open('./data/cifar10/wnids.txt') as f:
        wnids = [line.strip() for line in f.readlines()]

    tree = ET.parse('./data/cifar10/tree.xml');
    for wnid in wnids:
        node = tree.find('.//synset[@wnid="{}"]'.format(wnid))
        assert len(node.getchildren()) == 0, (
            node.get('words'), [child.get('words') for child in node.getchildren()]
        )

    print(' '.join([node.get('wnid') for node in tree.iter()
          if len(node.getchildren()) > 0 and node.get('wnid')]))
    exit()


device = 'cuda' if torch.cuda.is_available() else 'cpu'
best_acc = 0  # best test accuracy
start_epoch = 0  # start from epoch 0 or last checkpoint epoch

# Data
print('==> Preparing data..')
transform_train = transforms.Compose([
    transforms.RandomCrop(32, padding=4),
    transforms.RandomHorizontalFlip(),
    transforms.ToTensor(),
    transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
])

transform_test = transforms.Compose([
    transforms.ToTensor(),
    transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
])

if args.test_path_sanity or args.test_path:
    assert args.dataset in (CIFAR10PATHSANITY, CIFAR10PATH)

dataset_args = ()
if args.dataset == CIFAR10NODE:
    dataset = CIFAR10NodeDataset
    dataset_args = (args.wnid,)
elif args.dataset == CIFAR10PATHSANITY:
    dataset = CIFAR10PathSanityDataset
else:
    dataset = getattr(torchvision.datasets, args.dataset)

trainset = dataset(*dataset_args, root='./data', train=True, download=True, transform=transform_train)
testset = dataset(*dataset_args, root='./data', train=False, download=True, transform=transform_test)

trainloader = torch.utils.data.DataLoader(trainset, batch_size=args.batch_size, shuffle=True, num_workers=2)
testloader = torch.utils.data.DataLoader(testset, batch_size=100, shuffle=False, num_workers=2)

print(f'Training with dataset {args.dataset} and classes {trainset.classes}')

# Model
print('==> Building model..')
net = getattr(models, args.model)(
    num_classes=len(trainset.classes)
)
net = net.to(device)
if device == 'cuda':
    net = torch.nn.DataParallel(net)
    cudnn.benchmark = True

if args.test_path_sanity or args.test_path:
    net = models.linear(trainset.get_input_dim(), len(trainset.classes))

if args.test_path_sanity:
    net.set_weight(trainset.get_weights())

if args.resume:
    # Load checkpoint.
    print('==> Resuming from checkpoint..')
    assert os.path.isdir('checkpoint'), 'Error: no checkpoint directory found!'
    fname = generate_fname(args)
    checkpoint = torch.load('./checkpoint/{}.pth'.format(fname))
    net.load_state_dict(checkpoint['net'])
    best_acc = checkpoint['acc']
    start_epoch = checkpoint['epoch']

criterion = nn.CrossEntropyLoss()
optimizer = optim.SGD(net.parameters(), lr=args.lr, momentum=0.9, weight_decay=5e-4)

def adjust_learning_rate(epoch, lr):
    if epoch <= 150 / 350. * args.epochs:  # 32k iterations
      return lr
    elif epoch <= 250 / 350. * args.epochs:  # 48k iterations
      return lr/10
    else:
      return lr/100

# Training
def train(epoch):
    lr = adjust_learning_rate(epoch, args.lr)
    optimizer = optim.SGD(net.parameters(), lr=lr, momentum=0.9, weight_decay=5e-4)

    print('\nEpoch: %d' % epoch)
    net.train()
    train_loss = 0
    correct = 0
    total = 0
    for batch_idx, (inputs, targets) in enumerate(trainloader):
        inputs, targets = inputs.to(device), targets.to(device)
        optimizer.zero_grad()
        outputs = net(inputs)
        loss = criterion(outputs, targets)
        loss.backward()
        optimizer.step()

        train_loss += loss.item()
        _, predicted = outputs.max(1)
        total += targets.size(0)
        correct += predicted.eq(targets).sum().item()

        progress_bar(batch_idx, len(trainloader), 'Loss: %.3f | Acc: %.3f%% (%d/%d)'
            % (train_loss/(batch_idx+1), 100.*correct/total, correct, total))

def test(epoch, print_confusion_matrix, checkpoint=True):
    global best_acc
    net.eval()
    test_loss = 0
    correct = 0
    total = 0
    confusion_matrix = initialize_confusion_matrix(len(trainset.classes))
    with torch.no_grad():
        for batch_idx, (inputs, targets) in enumerate(testloader):
            inputs, targets = inputs.to(device), targets.to(device)
            outputs = net(inputs)
            loss = criterion(outputs, targets)

            test_loss += loss.item()
            _, predicted = outputs.max(1)
            total += targets.size(0)
            correct += predicted.eq(targets).sum().item()

            if device == 'cuda':
                predicted = predicted.cpu()
                targets = targets.cpu()
            predicted = predicted.numpy().ravel()
            targets = targets.numpy().ravel()
            confusion_matrix = update_confusion_matrix(confusion_matrix, predicted, targets)

            progress_bar(batch_idx, len(testloader), 'Loss: %.3f | Acc: %.3f%% (%d/%d)'
                % (test_loss/(batch_idx+1), 100.*correct/total, correct, total))

    # Save checkpoint.
    acc = 100.*correct/total
    if acc > best_acc and checkpoint:
        print('Saving..')
        state = {
            'net': net.state_dict(),
            'acc': acc,
            'epoch': epoch,
        }
        if not os.path.isdir('checkpoint'):
            os.mkdir('checkpoint')

        fname = generate_fname(args)
        torch.save(state, './checkpoint/{}.pth'.format(fname))
        best_acc = acc

    if print_confusion_matrix:
        set_np_printoptions()
        for row, cls in zip(confusion_matrix_recall(confusion_matrix), trainset.classes):
            print(row, cls)


if args.eval:
    if not args.resume:
        print(' * Warning: Model is not loaded from checkpoint. Use --resume')
    test(0, args.print_confusion_matrix, checkpoint=False)
    exit()


for epoch in range(start_epoch, args.epochs):
    train(epoch)
    test(epoch, args.print_confusion_matrix)
