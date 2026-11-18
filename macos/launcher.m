#import <Cocoa/Cocoa.h>

@interface ReviewLauncher : NSObject <NSApplicationDelegate>
@property(strong) NSTask *task;
@end

@implementation ReviewLauncher
- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    NSString *directory = NSBundle.mainBundle.bundlePath.stringByDeletingLastPathComponent;
    NSString *command = [directory stringByAppendingPathComponent:@"打开页畔.command"];
    if (![NSFileManager.defaultManager fileExistsAtPath:command]) {
        NSAlert *alert = [NSAlert new];
        alert.messageText = @"请将「页畔.app」放在「打开页畔.command」旁边";
        [alert runModal];
        [NSApp terminate:nil];
        return;
    }
    NSString *logPath = [command.stringByDeletingLastPathComponent stringByAppendingPathComponent:@"data/app-launch.log"];
    [NSFileManager.defaultManager createDirectoryAtPath:logPath.stringByDeletingLastPathComponent withIntermediateDirectories:YES attributes:nil error:nil];
    if (![NSFileManager.defaultManager fileExistsAtPath:logPath]) {
        [NSFileManager.defaultManager createFileAtPath:logPath contents:nil attributes:nil];
    }
    NSFileHandle *log = [NSFileHandle fileHandleForWritingAtPath:logPath];
    [log seekToEndOfFile];
    self.task = [NSTask new];
    self.task.executableURL = [NSURL fileURLWithPath:@"/bin/zsh"];
    self.task.arguments = @[command];
    self.task.standardOutput = log;
    self.task.standardError = log;
    self.task.terminationHandler = ^(NSTask *finished) {
        dispatch_async(dispatch_get_main_queue(), ^{
            if (finished.terminationStatus != 0) {
                NSAlert *alert = [NSAlert new];
                alert.messageText = @"页畔启动失败";
                alert.informativeText = [@"请查看启动日志：\n" stringByAppendingString:logPath];
                [alert runModal];
            }
            [NSApp terminate:nil];
        });
    };
    NSError *error;
    if (![self.task launchAndReturnError:&error]) {
        NSAlert *alert = [NSAlert new];
        alert.messageText = @"无法启动页畔";
        alert.informativeText = error.localizedDescription;
        [alert runModal];
        [NSApp terminate:nil];
    }
}
@end

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSApplication *application = NSApplication.sharedApplication;
        ReviewLauncher *delegate = [ReviewLauncher new];
        application.delegate = delegate;
        [application run];
    }
    return 0;
}
