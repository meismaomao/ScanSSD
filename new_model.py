import torch.nn as nn
from torch.autograd import Variable
from torch.autograd import Function
from box_utils import*
from _functions import Detect, PriorBox
from modules import L2Norm
import torchvision.transforms as transforms
import torchvision.models as models
from torch.utils.serialization import load_lua
import torch.backends.cudnn as cudnn
import os


class SSD(nn.Module):
    """Single Shot Multibox Architecture
    The network is composed of a base VGG network followed by the
    added multibox conv layers.  Each multibox layer branches into
        1) conv2d for class conf scores
        2) conv2d for localization predictions
        3) associated priorbox layer to produce default bounding
           boxes specific to the layer's feature map size.
    See: https://arxiv.org/pdf/1512.02325.pdf for more details.

    Args:
        features1: (nn.Sequential) VGG layers for input
            size of either 300 or 500
        phase: (string) Can be "test" or "train"
        size: (int) the SSD version for the input size. Can be 300 or 500.
            Defaul: 300
        num_classes: (int) the number of classes to score. Default: 21.
    """

    def __init__(self, phase, sz=300, num_classes=21):
        super(SSD, self).__init__()
        self.phase = phase
        self.size = sz
        self.num_classes = num_classes
        param=num_classes*3
        self.features1 = build_base(cfg[str(sz)] ,3)

        # TODO: Build the rest of the sequentials in a for loop.
        v = [0.1, 0.1, 0.2, 0.2] # variances
        ar = [1,1,2,1/2,3,1/3] # aspect ratios
        self.features2 = nn.Sequential(
            nn.MaxPool2d(kernel_size=2, stride=2),
            nn.Conv2d(512, 512, kernel_size=3, padding=1),
            nn.BatchNorm2d(512),
            nn.ReLU(inplace=True),
            nn.Conv2d(512, 512, kernel_size=3, padding=1),
            nn.BatchNorm2d(512),
            nn.ReLU(inplace=True),
            nn.Conv2d(512, 512, kernel_size=3, padding=1),
            nn.BatchNorm2d(512),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(kernel_size=3,stride=1,padding=1),
            nn.Conv2d(512,1024,kernel_size=3,padding=6,dilation=6),
            nn.BatchNorm2d(1024),
            nn.ReLU(inplace=True),
            nn.Conv2d(1024,1024,kernel_size=1),
            nn.BatchNorm2d(1024),
            nn.ReLU(inplace=True),
        )

        self.features3 = nn.Sequential(
            nn.Conv2d(1024, 256, kernel_size=1),
            nn.BatchNorm2d(256),
            nn.ReLU(inplace=True),
            nn.Conv2d(256, 512, kernel_size=3, padding=1, stride=2),
            nn.BatchNorm2d(512),
            nn.ReLU(inplace=True),
        )

        self.features4 = nn.Sequential(
            nn.Conv2d(512, 128, kernel_size=1),
            nn.BatchNorm2d(128),
            nn.ReLU(inplace=True),
            nn.Conv2d(128, 256, kernel_size=3, padding=1, stride=2),
            nn.BatchNorm2d(256),
        )

        self.features5 = nn.Sequential(
            nn.Conv2d(256, 128, kernel_size=1),
            nn.BatchNorm2d(128),
            nn.ReLU(inplace=True),
            nn.Conv2d(128, 256, kernel_size=3),
            nn.BatchNorm2d(256),
        )

        self.features6 = nn.Sequential(
            nn.Conv2d(256, 128, kernel_size=1),
            nn.BatchNorm2d(128),
            nn.Conv2d(128, 256, kernel_size=3),
            nn.BatchNorm2d(256),
        )

        self.L2Norm = L2Norm(512,20)

        self.l4_3 = nn.Conv2d(512, 4*4, kernel_size=3, padding=1)
        self.c4_3 = nn.Conv2d(512, 4*self.num_classes, kernel_size=3, padding=1)

        self.l7 = nn.Conv2d(1024, 6*4, kernel_size=3, padding=1)
        self.c7 = nn.Conv2d(1024, 6*self.num_classes, kernel_size=3, padding=1)

        self.l8_2 = nn.Conv2d(512, 6*4, kernel_size=3, padding=1)
        self.c8_2 = nn.Conv2d(512, 6*self.num_classes, kernel_size=3, padding=1)

        self.l9_2 = nn.Conv2d(256, 6*4, kernel_size=3, padding=1)
        self.c9_2 = nn.Conv2d(256, 6*self.num_classes, kernel_size=3, padding=1)

        self.l10_2 = nn.Conv2d(256, 4*4, kernel_size=3, padding=1)
        self.c10_2 = nn.Conv2d(256, 4*self.num_classes, kernel_size=3, padding=1)

        self.l11_2 = nn.Conv2d(256, 4*4, kernel_size=3, padding=1)
        self.c11_2 = nn.Conv2d(256, 4*self.num_classes, kernel_size=3, padding=1)

        self.softmax = nn.Softmax()
        self.detect = Detect(21, 0, 200, 0.01, 0.45, 400)

    def forward(self, x):
        """Applies network layers and ops on input image(s) x.

        Args:
            x: input image or batch of images. Shape: [batch,3*batch,300,300].

        Return:
            Depending on phase:
            test:
                Variable(tensor) of output class label predictions,
                confidence score, and corresponding location predictions for
                each object detected. Shape: [batch,topk,7]

            train:
                list of concat outputs from:
                    1: confidence layers, Shape: [batch*num_priors,num_classes]
                    2: localization layers, Shape: [batch,num_priors*4]
                    3: priorbox layers, Shape: [2,num_priors*4]
        """
        x = self.features1(x)
        y = self.L2Norm(x)

        b1 = [self.l4_3(y).permute(0,2,3,1), self.c4_3(y).permute(0,2,3,1)]
        p1 = self.p4_3(x)
        b1 = [o.view(o.contiguous().size(0),-1) for o in b1]
        x = self.features2(x)

        b2 = [self.l7(x).permute(0,2,3,1), self.c7(x).permute(0,2,3,1)]
        p2 = self.p7(x)
        b2 = [o.view(o.contiguous().size(0),-1) for o in b2]
        x = self.features3(x)

        b3 = [self.l8_2(x).permute(0,2,3,1), self.c8_2(x).permute(0,2,3,1)]
        p3 = self.p8_2(x)
        b3 = [o.view(o.contiguous().size(0),-1) for o in b3]
        x = self.features4(x)

        b4 = [self.l9_2(x).permute(0,2,3,1), self.c9_2(x).permute(0,2,3,1)]
        p4 = self.p9_2(x)
        b4 = [o.view(o.contiguous().size(0),-1) for o in b4]
        x = self.features5(x)

        b5 = [self.l10_2(x).permute(0,2,3,1), self.c10_2(x).permute(0,2,3,1)]
        p5 = self.p10_2(x)
        b5 = [o.view(o.contiguous().size(0),-1) for o in b5]
        x = self.pool6(x)

        b6 = [self.l11_2(x).permute(0,2,3,1), self.l12_2(x).permute(0,2,3,1)]
        p6 = self.p11_2(x)
        b6 = [o.view(o.contiguous().size(0),-1) for o in b6]
        loc_layers = torch.cat((b1[0],b2[0],b3[0],b4[0],b5[0],b6[0]),1)
        conf_layers = torch.cat((b1[1],b2[1],b3[1],b4[1],b5[1],b6[1]),1)
        box_layers = torch.cat((p1,p2,p3,p4,p5,p6), 2)

        if self.phase == "test":
            conf_layers = conf_layers.view(-1,21)
            conf_layers = self.softmax(conf_layers)
            output = self.detect(loc_layers,conf_layers,box_layers)
        else:
            conf_layers = conf_layers.view(conf_layers.size(0),-1,self.num_classes)
            loc_layers = loc_layers.view(loc_layers.size(0),-1,4)
            box_layers = box_layers.squeeze(0)
            output = (loc_layers, conf_layers, box_layers)
        return output


    # This function is very closely adapted from jcjohnson pytorch-vgg conversion script
    # https://github.com/jcjohnson/pytorch-vgg/blob/master/t7_to_state_dict.py
    def load_weights(self, base_file, norm_file = './weights/normWeights.t7'):
        py_modules = list(self.modules())
        next_py_idx = 0
        scale_weight = load_lua(norm_file).float()
        other, ext = os.path.splitext(base_file)
        if ext == '.t7':
            print('Loading lua model weights...')
            other = load_lua(base_file)
        else:
            print('Only .t7 is supported for now.')
            return
        #elif: ext == ''
        for i, t7_module in enumerate(other.modules):
            if not hasattr(t7_module, 'weight'):
                continue
            assert hasattr(t7_module, 'bias')
            while not hasattr(py_modules[next_py_idx], 'weight'):
                next_py_idx += 1
            py_module = py_modules[next_py_idx]
            next_py_idx += 1

            # The norm layer should be the only layer with 1d weight
            if(py_module.weight.data.dim() == 1):
                # print('%r Copying data from\n  %r to\n  %r' % (i-1, "L2norm", py_module))
                # py_module.weight.data.copy_(scale_weight)
                py_module = py_modules[next_py_idx]
                next_py_idx += 1
            assert(t7_module.weight.size() == py_module.weight.size())
            print('%r Copying data from\n  %r to\n  %r' % (i, t7_module, py_module))

            py_module.weight.data.copy_(t7_module.weight)
            assert(t7_module.bias.size() == py_module.bias.size())
            py_module.bias.data.copy_(t7_module.bias)
        py_modules[-14].weight.data.copy_(scale_weight)
        print('%r Copying data from\n  %r to\n  %r' % (i-1, "L2norm", py_modules[-14]))


# This function is derived from torchvision VGG make_layers()
# https://github.com/pytorch/vision/blob/master/torchvision/models/vgg.py
def build_base(cfg, i, batch_norm=True):
    layers = []
    in_channels = i
    for v in cfg:
        if v == 'M':
            layers += [nn.MaxPool2d(kernel_size=2, stride=2)]
        elif v == 'C':
            layers += [nn.MaxPool2d(kernel_size=2, stride=2, ceil_mode=True)]
        else:
            conv2d = nn.Conv2d(in_channels, v, kernel_size=3, padding=1)
            if batch_norm:
                layers += [conv2d, nn.BatchNorm2d(v), nn.ReLU(inplace=True)]
            else:
                layers += [conv2d, nn.ReLU(inplace=True)]
            in_channels = v
    return nn.Sequential(*layers)


cfg = {
    "300" : [64, 64, 'M', 128, 128, 'M', 256, 256, 256, 'C', 512, 512, 512],
}


def build_ssd(phase, size, num_classes):
    if phase != "test" and phase != "train":
        print("Error: Phase not recognized")
        return
    if size != 300:
        print("Error: Sorry only SSD300 is supported currently!")
    return SSD(phase, size, num_classes)
