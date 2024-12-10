from torch.utils.tensorboard import SummaryWriter
from torchvision import transforms
import torchvision
import csv
from quant_net import *
from training_utils import *

def main():
    torch.manual_seed(23)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    cudnn.benchmark = True
    cudnn.deterministic = True

    ws_statistics = {}

    args = args_config.get_args()
    print("********** SNN simulation parameters **********")
    print(args)

    if args.dataset == 'cifar10':
        transform_train = transforms.Compose([
            transforms.RandomCrop(32, padding=4),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010))
        ])

        transform_test = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010))
        ])

        train_dataset = torchvision.datasets.CIFAR10(
            root=args.dataset_dir,
            train=True,
            transform=transform_train,
            download=True)

        test_dataset = torchvision.datasets.CIFAR10(
            root=args.dataset_dir,
            train=False,
            transform=transform_test,
            download=True)

        train_data_loader = torch.utils.data.DataLoader(
            dataset=train_dataset,
            batch_size=16,  # 修改为更高的 batch_size
            shuffle=True,
            drop_last=True,
            num_workers=12,
            pin_memory=True)
        test_data_loader = torch.utils.data.DataLoader(
            dataset=test_dataset,
            batch_size=128,
            shuffle=True,
            drop_last=False,
            num_workers=4,
            pin_memory=True)

    def hook_fn(module, input, output):
        """
        钩子函数用于统计每次卷积操作中的 W * S 次数。
        参数:
        - module: 触发钩子的模块
        - input: 输入张量
        - output: 输出张量
        """

        nonlocal ws_statistics

        # 获取权重和输入
        weights, _ = w_q_inference(module.conv_module.weight, module.num_bits_w, module.beta[0])
        inputs = input[0]

        # 确保数据在相同设备上，避免不必要的数据传输
        weights = weights.to(inputs.device)

        # 使用 unfold 操作将输入展平为卷积块的形式
        unfold = torch.nn.Unfold(kernel_size=weights.shape[2:], padding=module.conv_module.padding, stride=module.conv_module.stride)
        input_patches = unfold(inputs)  # 展开为形状为 [batch_size, C_in * H_k * W_k, L]

        # 重塑权重形状为 [C_out, C_in * H_k * W_k]
        weights = weights.view(weights.shape[0], -1)

        # 扩展权重维度以便进行广播
        weights_expanded = weights.unsqueeze(0).unsqueeze(-1)
        input_patches_expanded = input_patches.unsqueeze(1)

        # 创建掩码以找到输入中的非零值
        input_nonzero_mask = input_patches_expanded != 0

        # 统计权重为 +1 且输入不为 0 的乘积次数
        count_w_pos_s = torch.sum((weights_expanded == 1) & input_nonzero_mask, dim=[2, 3])

        # 统计权重为 -1 且输入不为 0 的乘积次数
        count_w_neg_s = torch.sum((weights_expanded == -1) & input_nonzero_mask, dim=[2, 3])

        # 获取模块名称
        module_name = next(name for name, mod in model.named_modules() if mod is module)

        # 仅统计特定的层（ConvLif2, ConvLif3, ConvLif4, ConvLif5, ConvLif6）
        if module_name in ['ConvLif2', 'ConvLif3', 'ConvLif4', 'ConvLif5', 'ConvLif6']:
            # 初始化统计字典（如果尚未初始化）
            if module_name not in ws_statistics:
                ws_statistics[module_name] = {'W=1 & S!=0': 0, 'W=-1 & S!=0': 0}

            # 更新统计信息
            ws_statistics[module_name]['W=1 & S!=0'] += count_w_pos_s.sum().item()
            ws_statistics[module_name]['W=-1 & S!=0'] += count_w_neg_s.sum().item()

    # ********************************* other visualization ****************************
    model = Q_ShareScale_VGG8(args.T, args.dataset).to(device)

    # 注册钩子函数到模型中的特定 QConv2dLIF 层（ConvLif2, ConvLif3, ConvLif4, ConvLif5, ConvLif6）
    hooks = []
    for name, module in model.named_modules():
        if isinstance(module, QConv2dLIF) and name in ['ConvLif2', 'ConvLif3', 'ConvLif4', 'ConvLif5', 'ConvLif6']:
            hooks.append(module.register_forward_hook(hook_fn))

    # ******************************load pre-train model********************************
    pretrained_path = f"{os.getcwd()}/pretrain_models/temp/99_dict_2w2b_if_hard_fake.pth"
    if os.path.exists(pretrained_path):
        print(f"Loading pretrained weights from {pretrained_path}")
        pretrained_model = torch.load(pretrained_path)
        if isinstance(pretrained_model, dict):
            model.load_state_dict(pretrained_model)
            print("Model loaded. First layer weights mean:", next(model.parameters()).mean().item())
        else:
            print(f"Unexpected type of loaded model: {type(pretrained_model)}")
    else:
        print(f"Pretrained weights not found at {pretrained_path}, train from scratch.")

    # ****************************** Training Loop ********************************
    criterion = nn.CrossEntropyLoss()
    optimizer = torch.optim.SGD(model.parameters(), args.lr, momentum=0.9, weight_decay=5e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epoch, eta_min=0)

    model.train()
    for epoch in range(args.epoch):
        for batch_idx, (imgs, targets) in enumerate(train_data_loader):
            if batch_idx == 0:  # 仅处理第一个 batch
                imgs, targets = imgs.to(device), targets.to(device)
                optimizer.zero_grad()
                outputs = model(imgs)
                loss = sum([criterion(s, targets) for s in outputs]) / args.T
                loss.backward()
                optimizer.step()
                break  # 只处理一个 batch，跳出循环

        # 调整学习率
        scheduler.step()
        break  # 只处理一个 epoch，跳出循环

    # 移除钩子
    for hook in hooks:
        hook.remove()

    # ****************************** Save Statistics ********************************
    output_csv_path = os.path.join('test_plots', 'w_s_operation', 'ws_statistics.csv')
    os.makedirs(os.path.dirname(output_csv_path), exist_ok=True)

    # 只保存特定 ConvLIF 层的累计 W * S 操作和
    with open(output_csv_path, mode='w', newline='') as csv_file:
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(['Layer Name', 'W=1 & S!=0', 'W=-1 & S!=0'])
        for module_name, stats in ws_statistics.items():
            if stats:  # 确保统计数据存在
                csv_writer.writerow([module_name, stats.get('W=1 & S!=0', 0), stats.get('W=-1 & S!=0', 0)])
    print("Ws computation statistics saved!")



if __name__ == '__main__':
    main()