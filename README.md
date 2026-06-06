# Sunflowyr Engine

Sunflowyr Engine is an upcoming high end modding toolkit for RWBY Grimm Eclipse.

It's a modding engine i'm developing to make modding RWBY GE significantly easier and pleasant to use. It's RWBY inspired.

Sunflowyr Engine will also include a Mod Creator and deluxe Mod Manager for usage as well. The Mod Creator will turn modded files into .RWBYGEP (mod package) or .RWBYGEI (mod installer) files to be used with the Mod Manager, they're custom mod formats I designed. RWBYGEP is ideal for mods that don't require choosing what to install while RWBYGEI is preferable when modders want to allow gamers to choose what parts of the mod to install.

# Details

It unpacks everything from the RWBY GE containers (all 400k+ files), assigns taildata (a method I use for safe mod appending/disabling) to unpacked files, converts various formats to easier to edit formats (FBX, GLB, PNG, wav, etc) but it still keeps the original Unity formats incase modders want to work with the Unity format versions, etc.

Its purpose is to unpack everything, ensure modded files can be properly applied to the containers by appending mods and auto updating metadata, and ensure safe mod disabling.

There is no size limits for mods, you aren't required to keep mods the same size as the original files, dynamic file sizes are supported so whether your mods are smaller/larger doesn't matter.

# The workflow for modders/mod users

The modding workflow for modders will be like this:

Unpack the game, mod whatever files, turn modded files into mod packages/installers (custom mod formats I designed which are .RWBYGEP and RWBYGEI) with the Mod Creator (similar to my Aldnoah Engine where you turn modded files into compatible mod formats with the Mod Manager), and then apply/disable as desired with the Mod Manager

The modding workflow for users that don't want to mod but just use mods will be like this:

Download mods you want, place the mods in the Mods folder, and apply/disable as desired.

# GUI examples of the main hub

<img width="1390" height="708" alt="se1" src="https://github.com/user-attachments/assets/e3686662-22e2-429d-ac6a-bfbfb50969e1" />

<img width="1386" height="675" alt="se2" src="https://github.com/user-attachments/assets/9dd0e40e-cfc2-460c-a460-4dbbd123eb3f" />

<img width="1365" height="682" alt="se3" src="https://github.com/user-attachments/assets/c7cd7c8e-20a3-454d-8f7b-fe2da6cdd3ac" />

<img width="1331" height="690" alt="se4" src="https://github.com/user-attachments/assets/81c49d18-ced5-479c-9938-9e72e3804fab" />

<img width="1345" height="656" alt="se5" src="https://github.com/user-attachments/assets/a3639760-98e0-4dfa-a045-64de43f1c832" />

<img width="1377" height="669" alt="se6" src="https://github.com/user-attachments/assets/57d899bd-1534-4d7c-b84b-9eb59c6215d3" />
