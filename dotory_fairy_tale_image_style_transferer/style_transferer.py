from PIL import Image
import torchvision.transforms as transforms
import torch
import torchvision.models as models
import torch.optim as optim
import torch.nn as nn
from .loss import ContentLoss, StyleLoss, Normalization

class StyleTransferer:
    def __init__(self):
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.cnn = models.vgg19(pretrained=True).features.to(self.device).eval()

        self.normalization_mean = torch.tensor([0.485, 0.456, 0.406]).to(self.device)
        self.normalization_std = torch.tensor([0.229, 0.224, 0.225]).to(self.device)

        self.unloader = transforms.ToPILImage()

    def transfer(self, style_image, content_iamge):
        long_length_index = 0 if content_iamge.size[0] > content_iamge.size[1] else 1
        short_length_index = 1 if long_length_index == 0 else 0

        long_length = content_iamge.size[long_length_index]
        short_length = content_iamge.size[short_length_index]

        max_length = 512

        short_length = short_length if long_length <= max_length else int(short_length * max_length / long_length)
        long_length = long_length if long_length <= max_length else max_length

        new_size = [-1, -1]

        new_size[long_length_index] = long_length
        new_size[short_length_index] = short_length

        style_loader = transforms.Compose([
            transforms.Resize(long_length),
            transforms.CenterCrop(new_size),
            transforms.ToTensor()]
        )

        content_loader = transforms.Compose([
            transforms.Resize(new_size),
            transforms.ToTensor()]
        )

        style_image = style_loader(style_image).unsqueeze(0).to(self.device, torch.float)
        content_iamge = content_loader(content_iamge).unsqueeze(0).to(self.device, torch.float)

        assert style_image.size() == content_iamge.size()

        input_image = content_iamge.clone()

        style_transfered_image =  self._run_style_transfer(
            self.cnn, self.normalization_mean, self.normalization_std,
            content_iamge, style_image, input_image,
        )

        style_transfered_image = style_transfered_image.cpu().clone()  # we clone the tensor to not do changes on it
        style_transfered_image = style_transfered_image.squeeze(0)      # remove the fake batch dimension
        style_transfered_image = self.unloader(style_transfered_image)

        return style_transfered_image
    
    def _run_style_transfer(self, cnn, normalization_mean, normalization_std,
        content_image, style_image, input_image, num_steps=300,
        style_weight=1000000, content_weight=1
    ):
        model, style_losses, content_losses = self._get_style_model_and_losses(cnn,
            normalization_mean, normalization_std, style_image, content_image)

        # We want to optimize the input and not the model parameters so we
        # update all the requires_grad fields accordingly
        input_image.requires_grad_(True)
        model.requires_grad_(False)

        optimizer = self._get_input_optimizer(input_image)

        run = [0]
        while run[0] <= num_steps:
            def closure():
                # correct the values of updated input image
                with torch.no_grad():
                    input_image.clamp_(0, 1)

                optimizer.zero_grad()
                model(input_image)
                style_score = 0
                content_score = 0

                for sl in style_losses:
                    style_score += sl.loss
                for cl in content_losses:
                    content_score += cl.loss

                style_score *= style_weight
                content_score *= content_weight

                loss = style_score + content_score
                loss.backward()

                run[0] += 1
                # if run[0] % 50 == 0:
                #     print("run {}:".format(run))
                #     print('Style Loss : {:4f} Content Loss: {:4f}'.format(
                #         style_score.item(), content_score.item()))
                #     print()

                return style_score + content_score

            optimizer.step(closure)

        # a last correction...
        with torch.no_grad():
            input_image.clamp_(0, 1)

        return input_image

    def _get_style_model_and_losses(
        self, cnn, normalization_mean, normalization_std, style_image, content_image,
    ):
        content_layers = ['conv_4']
        style_layers = ['conv_1', 'conv_2', 'conv_3', 'conv_4', 'conv_5']

        # normalization module
        normalization = Normalization(normalization_mean, normalization_std).to(self.device)

        # just in order to have an iterable access to or list of content/syle
        # losses
        content_losses = []
        style_losses = []

        # assuming that cnn is a nn.Sequential, so we make a new nn.Sequential
        # to put in modules that are supposed to be activated sequentially
        model = nn.Sequential(normalization)

        i = 0  # increment every time we see a conv
        for layer in cnn.children():
            if isinstance(layer, nn.Conv2d):
                i += 1
                name = 'conv_{}'.format(i)
            elif isinstance(layer, nn.ReLU):
                name = 'relu_{}'.format(i)
                # The in-place version doesn't play very nicely with the ContentLoss
                # and StyleLoss we insert below. So we replace with out-of-place
                # ones here.
                layer = nn.ReLU(inplace=False)
            elif isinstance(layer, nn.MaxPool2d):
                name = 'pool_{}'.format(i)
            elif isinstance(layer, nn.BatchNorm2d):
                name = 'bn_{}'.format(i)
            else:
                raise RuntimeError('Unrecognized layer: {}'.format(layer.__class__.__name__))

            model.add_module(name, layer)

            if name in content_layers:
                # add content loss:
                target = model(content_image).detach()
                content_loss = ContentLoss(target)
                model.add_module("content_loss_{}".format(i), content_loss)
                content_losses.append(content_loss)

            if name in style_layers:
                # add style loss:
                target_feature = model(style_image).detach()
                style_loss = StyleLoss(target_feature)
                model.add_module("style_loss_{}".format(i), style_loss)
                style_losses.append(style_loss)

        # now we trim off the layers after the last content and style losses
        for i in range(len(model) - 1, -1, -1):
            if isinstance(model[i], ContentLoss) or isinstance(model[i], StyleLoss):
                break

        model = model[:(i + 1)]

        return model, style_losses, content_losses

    def _get_input_optimizer(self, input_image):
        # this line to show that input is a parameter that requires a gradient
        optimizer = optim.LBFGS([input_image])
        return optimizer
